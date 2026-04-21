"""
Compliance / audit endpoints:
  - GET /portfolios/{id}/form-8949.csv  — Form 8949-shaped CSV
  - GET /compliance/exam-export         — recommendations + audit + transactions
    for SEC/state exam requests, over a date range (admin-only)
"""
import csv
import io
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ...auth import require_admin, get_current_user
from ...database import get_db
from ...models.models import (
    Portfolio, Position, TaxLot, Transaction, RecommendationLog, AuditEvent, Client,
)
from ...services.tax_pdf import build_tax_report_pdf
from ..deps import assert_portfolio_access

router = APIRouter(tags=["compliance"])


@router.get("/portfolios/{portfolio_id}/form-8949.csv")
async def form_8949_csv(
    portfolio_id: int,
    year: int | None = Query(None, description="Tax year (defaults to current UTC year)"),
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """
    Form 8949 CSV: one row per closed tax lot, with IRS-matching columns.
    Box A/D mapping (cost basis reported to IRS) is not inferable here — the
    customer fills that in on their actual Form 8949; we provide the detail rows.
    """
    target_year = year or datetime.utcnow().year
    result = await db.execute(
        select(TaxLot, Position.symbol)
        .join(Position, TaxLot.position_id == Position.id)
        .where(
            Position.portfolio_id == portfolio.id,
            TaxLot.sale_date.isnot(None),
        )
        .order_by(TaxLot.sale_date)
    )
    rows = result.all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "Form", "Description",
        "Date Acquired (mm/dd/yyyy)", "Date Sold (mm/dd/yyyy)",
        "Proceeds (d)", "Cost Basis (e)",
        "Wash-Sale Code", "Wash-Sale Adjustment (g)",
        "Gain or Loss (h)",
        "Holding Period",
    ])
    for lot, symbol in rows:
        if lot.sale_date.year != target_year:
            continue
        holding_days = (lot.sale_date - lot.purchase_date).days
        term = "Long-term" if holding_days >= 365 else "Short-term"
        cost = round(lot.cost_basis * (lot.shares or 0), 2)
        wash = round(lot.wash_sale_disallowed or 0.0, 2)
        gl = round(lot.realized_gain_loss or 0.0, 2)
        description = f"{lot.shares:g} shares {symbol}"
        w.writerow([
            "8949",
            description,
            lot.purchase_date.strftime("%m/%d/%Y"),
            lot.sale_date.strftime("%m/%d/%Y"),
            round(lot.proceeds or 0.0, 2),
            cost,
            "W" if wash > 0 else "",
            wash if wash > 0 else "",
            gl,
            term,
        ])
    out.seek(0)
    filename = f"portfolio_{portfolio.id}_form_8949_{target_year}.csv"
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/portfolios/{portfolio_id}/tax-report.pdf")
async def tax_report_pdf(
    portfolio_id: int,
    year: int | None = Query(None),
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """Client-ready tax report PDF in Form 8949 layout."""
    target_year = year or datetime.utcnow().year
    result = await db.execute(
        select(TaxLot, Position.symbol)
        .join(Position, TaxLot.position_id == Position.id)
        .where(
            Position.portfolio_id == portfolio.id,
            TaxLot.sale_date.isnot(None),
        )
        .order_by(TaxLot.sale_date)
    )
    rows = [(lot, sym) for lot, sym in result.all() if lot.sale_date.year == target_year]

    client_name = "Individual"
    if portfolio.client_id:
        c = await db.get(Client, portfolio.client_id)
        client_name = c.name if c else client_name

    pdf_bytes = build_tax_report_pdf(
        portfolio_name=portfolio.name,
        client_name=client_name,
        tax_year=target_year,
        lots=rows,
    )
    filename = f"portfolio_{portfolio.id}_tax_report_{target_year}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/compliance/exam-export")
async def exam_export(
    start: str = Query(..., description="ISO date, inclusive"),
    end: str = Query(..., description="ISO date, inclusive"),
    user_id: int | None = Query(None),
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Bundle everything needed for an SEC/state exam over a date range: every
    recommendation, audit event, and transaction. Returns JSON.
    """
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "start/end must be ISO dates (YYYY-MM-DD)")

    # Recommendations
    r_stmt = select(RecommendationLog).where(
        RecommendationLog.created_at >= start_dt,
        RecommendationLog.created_at <= end_dt,
    )
    if user_id is not None:
        r_stmt = r_stmt.where(RecommendationLog.user_id == user_id)
    recs = (await db.execute(r_stmt)).scalars().all()

    # Audit events
    a_stmt = select(AuditEvent).where(
        AuditEvent.created_at >= start_dt,
        AuditEvent.created_at <= end_dt,
    )
    if user_id is not None:
        a_stmt = a_stmt.where(AuditEvent.user_id == user_id)
    audits = (await db.execute(a_stmt)).scalars().all()

    # Transactions (optionally scoped by user's own portfolios)
    t_stmt = select(Transaction).where(
        Transaction.timestamp >= start_dt,
        Transaction.timestamp <= end_dt,
    )
    txns = (await db.execute(t_stmt)).scalars().all()

    return {
        "range": {"start": start, "end": end},
        "user_id_filter": user_id,
        "recommendations": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "portfolio_id": r.portfolio_id,
                "prompt": r.prompt,
                "model_version": r.model_version,
                "prompt_version": r.prompt_version,
                "adv_version_acknowledged": r.adv_version_acknowledged,
                "demo_mode": r.demo_mode,
                "created_at": r.created_at.isoformat(),
                "reasoning": r.reasoning,
                "draft_plan": json.loads(r.draft_plan_json) if r.draft_plan_json else None,
                "tool_calls": json.loads(r.tool_calls_json) if r.tool_calls_json else [],
            }
            for r in recs
        ],
        "audit_events": [
            {
                "id": a.id, "user_id": a.user_id, "event_type": a.event_type,
                "portfolio_id": a.portfolio_id,
                "object_type": a.object_type, "object_id": a.object_id,
                "details": json.loads(a.details_json) if a.details_json else None,
                "ip_address": a.ip_address,
                "created_at": a.created_at.isoformat(),
            }
            for a in audits
        ],
        "transactions": [
            {
                "id": t.id, "portfolio_id": t.portfolio_id, "symbol": t.symbol,
                "transaction_type": t.transaction_type, "shares": t.shares,
                "price": t.price, "total_value": t.total_value,
                "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                "notes": t.notes,
            }
            for t in txns
        ],
    }
