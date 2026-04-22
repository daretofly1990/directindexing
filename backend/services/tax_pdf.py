"""
Tax report PDF generator — Form 8949 / Schedule D layout.

Produces a polished PDF with:
  - Cover page: client name, tax year, summary totals, disclosure text
  - Short-term detail pages (Form 8949 Part I shape)
  - Long-term detail pages (Form 8949 Part II shape)
  - Wash-sale adjustment column with "W" code flagged

Intended as a client-ready document. Tax prep software can ingest the CSV;
the PDF is for the client to file alongside or hand to their CPA.
"""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
)

from .disclosures import TAX_REPORT_DISCLOSURE


def _row_for_lot(lot, symbol: str) -> list:
    cost_total = round(lot.cost_basis * (lot.shares or 0), 2)
    wash = round(lot.wash_sale_disallowed or 0.0, 2)
    gl = round(lot.realized_gain_loss or 0.0, 2)
    desc = f"{lot.shares:g} sh {symbol}"
    return [
        desc,
        lot.purchase_date.strftime("%m/%d/%Y"),
        lot.sale_date.strftime("%m/%d/%Y"),
        f"${lot.proceeds or 0:,.2f}",
        f"${cost_total:,.2f}",
        "W" if wash > 0 else "",
        f"${wash:,.2f}" if wash > 0 else "",
        f"${gl:,.2f}",
    ]


def build_tax_report_pdf(
    portfolio_name: str,
    client_name: str,
    tax_year: int,
    lots: list[tuple],   # list of (TaxLot, symbol)
) -> bytes:
    """
    Return PDF bytes. `lots` is [(TaxLot, symbol)] — only closed lots for the
    target year should be passed in.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(letter),
        leftMargin=0.4 * inch, rightMargin=0.4 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"Tax Report {tax_year} — {client_name}",
    )
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    body = styles["BodyText"]
    disclosure_style = ParagraphStyle(
        "disc", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#b45309"),
    )

    story = []

    # Cover
    story.append(Paragraph(f"Tax Report — {tax_year}", title_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(f"<b>Client:</b> {client_name}", body))
    story.append(Paragraph(f"<b>Portfolio:</b> {portfolio_name}", body))
    story.append(Paragraph(
        f"<b>Generated:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", body,
    ))
    story.append(Spacer(1, 0.2 * inch))

    # Summary totals
    st_gain = st_loss = lt_gain = lt_loss = wash_total = 0.0
    st_rows = []
    lt_rows = []
    for lot, sym in lots:
        gl = lot.realized_gain_loss or 0.0
        wash_total += lot.wash_sale_disallowed or 0.0
        holding_days = (lot.sale_date - lot.purchase_date).days
        row = _row_for_lot(lot, sym)
        if holding_days >= 365:
            (lt_gain if gl >= 0 else lt_loss)
            if gl >= 0:
                lt_gain += gl
            else:
                lt_loss += gl
            lt_rows.append(row)
        else:
            if gl >= 0:
                st_gain += gl
            else:
                st_loss += gl
            st_rows.append(row)
    net_st = st_gain + st_loss
    net_lt = lt_gain + lt_loss
    total = net_st + net_lt

    summary_tbl = Table(
        [
            ["", "Gains", "Losses", "Net"],
            ["Short-term",
             f"${st_gain:,.2f}", f"${st_loss:,.2f}", f"${net_st:,.2f}"],
            ["Long-term",
             f"${lt_gain:,.2f}", f"${lt_loss:,.2f}", f"${net_lt:,.2f}"],
            ["Wash-sale disallowed", "", "", f"${wash_total:,.2f}"],
            ["Total recognizable", "", "", f"${total:,.2f}"],
        ],
        colWidths=[2.2 * inch, 1.4 * inch, 1.4 * inch, 1.6 * inch],
    )
    summary_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
        ("FONT", (0, 1), (0, -1), "Helvetica-Bold", 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#64748b")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f1f5f9")),
        ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 10),
    ]))
    story.append(summary_tbl)
    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("<b>Disclosure</b>", body))
    story.append(Paragraph(TAX_REPORT_DISCLOSURE, disclosure_style))

    # Detail pages
    header = [
        "Description", "Date Acquired", "Date Sold",
        "Proceeds (d)", "Cost Basis (e)", "Wash Code", "Wash Adj (g)", "Gain/Loss (h)",
    ]
    col_widths = [1.8 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch, 1.1 * inch,
                  0.7 * inch, 1.0 * inch, 1.1 * inch]

    def _section(rows, title):
        if not rows:
            return []
        out = [PageBreak(),
               Paragraph(title, styles["Heading2"]),
               Spacer(1, 0.1 * inch)]
        tbl = Table([header] + rows, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 7),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        out.append(tbl)
        return out

    story += _section(st_rows, f"Form 8949 Part I — Short-term (under 1 year)  [{len(st_rows)} rows]")
    story += _section(lt_rows, f"Form 8949 Part II — Long-term (1+ year)  [{len(lt_rows)} rows]")

    doc.build(story)
    return buf.getvalue()
