"""
Broker lot-level CSV importer.

Supported formats (auto-detected from column headers):
  Schwab  — "Gain/Loss" export from the Cost Basis tab
  Fidelity — Cost Basis CSV export from Portfolio

Both formats produce rows with: symbol, date_acquired, quantity, cost_per_share.
"""
import csv
import io
import re
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Portfolio, Position, TaxLot
from .lot_engine import lot_engine


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _clean_number(raw: str) -> float:
    """Strip '$', commas, parentheses (negatives), '%' and convert to float."""
    s = raw.strip().replace("$", "").replace(",", "").replace("%", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s) if s else 0.0


_DATE_FMTS = ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%b %d, %Y", "%d-%b-%Y"]


def _parse_date(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {raw!r}")


def _norm_header(h: str) -> str:
    """Lower-case, strip quotes and extra whitespace."""
    return h.strip().strip('"').lower()


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

BrokerFormat = Literal["schwab", "fidelity", "generic"]


def _detect_format(headers: list[str]) -> BrokerFormat:
    joined = " ".join(headers)
    if "date acquired" in joined and "cost per share" in joined:
        if "closing price" in joined or "gain/loss per share" in joined:
            return "schwab"
        return "fidelity"
    if "lot date" in joined:
        return "fidelity"
    return "generic"


def _col(headers: list[str], *candidates: str) -> int:
    """Return first index matching any candidate (case-insensitive substring)."""
    for cand in candidates:
        for i, h in enumerate(headers):
            if cand in h:
                return i
    return -1


# ---------------------------------------------------------------------------
# Row parsers per broker
# ---------------------------------------------------------------------------

def _parse_schwab_rows(headers: list[str], rows: list[list[str]]) -> list[dict]:
    """Parse rows from a Schwab Gain/Loss CSV export."""
    idx_sym   = _col(headers, "symbol")
    idx_date  = _col(headers, "date acquired")
    idx_qty   = _col(headers, "quantity")
    idx_cost  = _col(headers, "cost per share")

    if any(i == -1 for i in [idx_sym, idx_date, idx_qty, idx_cost]):
        raise ValueError(f"Missing required Schwab columns. Found: {headers}")

    lots = []
    for row in rows:
        if len(row) <= max(idx_sym, idx_date, idx_qty, idx_cost):
            continue
        symbol = row[idx_sym].strip().strip('"').upper()
        if not symbol or symbol in ("-", ""):
            continue
        try:
            lots.append({
                "symbol": symbol,
                "date_acquired": _parse_date(row[idx_date]),
                "shares": _clean_number(row[idx_qty]),
                "cost_per_share": _clean_number(row[idx_cost]),
            })
        except (ValueError, IndexError):
            continue
    return lots


def _parse_fidelity_rows(headers: list[str], rows: list[list[str]]) -> list[dict]:
    """Parse rows from a Fidelity Cost Basis CSV export."""
    idx_sym  = _col(headers, "symbol")
    idx_date = _col(headers, "lot date", "date acquired")
    idx_qty  = _col(headers, "quantity")
    idx_cost = _col(headers, "cost per share")

    if any(i == -1 for i in [idx_sym, idx_date, idx_qty, idx_cost]):
        raise ValueError(f"Missing required Fidelity columns. Found: {headers}")

    lots = []
    for row in rows:
        if len(row) <= max(idx_sym, idx_date, idx_qty, idx_cost):
            continue
        symbol = row[idx_sym].strip().strip('"').upper()
        if not symbol or symbol in ("-", ""):
            continue
        try:
            lots.append({
                "symbol": symbol,
                "date_acquired": _parse_date(row[idx_date]),
                "shares": _clean_number(row[idx_qty]),
                "cost_per_share": _clean_number(row[idx_cost]),
            })
        except (ValueError, IndexError):
            continue
    return lots


def _parse_generic_rows(headers: list[str], rows: list[list[str]]) -> list[dict]:
    """Best-effort parser for unknown CSV formats with recognised column names."""
    idx_sym  = _col(headers, "symbol", "ticker")
    idx_date = _col(headers, "date acquired", "lot date", "acquired", "purchase date")
    idx_qty  = _col(headers, "quantity", "shares", "qty")
    idx_cost = _col(headers, "cost per share", "cost/share", "unit cost", "avg cost")

    if any(i == -1 for i in [idx_sym, idx_date, idx_qty, idx_cost]):
        raise ValueError(
            f"Cannot determine required columns. Found: {headers}. "
            "Expected columns: symbol, date acquired, quantity, cost per share."
        )

    lots = []
    for row in rows:
        if len(row) <= max(idx_sym, idx_date, idx_qty, idx_cost):
            continue
        symbol = row[idx_sym].strip().strip('"').upper()
        if not symbol:
            continue
        try:
            lots.append({
                "symbol": symbol,
                "date_acquired": _parse_date(row[idx_date]),
                "shares": _clean_number(row[idx_qty]),
                "cost_per_share": _clean_number(row[idx_cost]),
            })
        except (ValueError, IndexError):
            continue
    return lots


# ---------------------------------------------------------------------------
# Public parse function
# ---------------------------------------------------------------------------

def parse_lot_csv(content: str | bytes) -> tuple[BrokerFormat, list[dict]]:
    """
    Parse a broker lot-level CSV string/bytes.
    Returns (detected_format, list_of_lot_dicts).
    Each lot dict: {symbol, date_acquired, shares, cost_per_share}.
    Raises ValueError if the format cannot be parsed.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")

    reader = csv.reader(io.StringIO(content))
    raw_rows = list(reader)

    # Find the header row — skip Schwab/Fidelity preamble lines
    header_idx = 0
    for i, row in enumerate(raw_rows):
        normalized = [_norm_header(c) for c in row]
        if any(kw in " ".join(normalized) for kw in ("symbol", "ticker")):
            header_idx = i
            break

    headers = [_norm_header(c) for c in raw_rows[header_idx]]
    data_rows = raw_rows[header_idx + 1:]

    fmt = _detect_format(headers)

    if fmt == "schwab":
        lots = _parse_schwab_rows(headers, data_rows)
    elif fmt == "fidelity":
        lots = _parse_fidelity_rows(headers, data_rows)
    else:
        lots = _parse_generic_rows(headers, data_rows)

    return fmt, lots


# ---------------------------------------------------------------------------
# Database import
# ---------------------------------------------------------------------------

async def import_lots_to_portfolio(
    db: AsyncSession,
    portfolio_id: int,
    lots: list[dict],
    overwrite_existing: bool = False,
) -> dict:
    """
    Persist parsed lot dicts into the database for the given portfolio.

    If a Position already exists for a symbol, new lots are appended.
    If overwrite_existing=True, existing open lots for the symbol are deleted first.

    Returns a summary dict.
    """
    portfolio = await db.get(Portfolio, portfolio_id)
    if not portfolio:
        raise ValueError("Portfolio not found")

    imported = 0
    skipped = 0
    symbols_touched: set[str] = set()

    # Group by symbol
    by_symbol: dict[str, list[dict]] = {}
    for lot in lots:
        if lot["shares"] <= 0 or lot["cost_per_share"] <= 0:
            skipped += 1
            continue
        by_symbol.setdefault(lot["symbol"], []).append(lot)

    for symbol, sym_lots in by_symbol.items():
        # Find or create Position
        pos_result = await db.execute(
            select(Position).where(
                Position.portfolio_id == portfolio_id,
                Position.symbol == symbol,
                Position.is_active == True,  # noqa: E712
            )
        )
        pos = pos_result.scalar_one_or_none()

        if pos and overwrite_existing:
            # Delete existing open lots for this position
            lot_result = await db.execute(
                select(TaxLot).where(
                    TaxLot.position_id == pos.id,
                    TaxLot.sale_date == None,  # noqa: E711
                )
            )
            for existing_lot in lot_result.scalars().all():
                await db.delete(existing_lot)

        if not pos:
            total_shares = sum(l["shares"] for l in sym_lots)
            total_cost = sum(l["shares"] * l["cost_per_share"] for l in sym_lots)
            avg_cost = total_cost / total_shares if total_shares > 0 else 0.0
            pos = Position(
                portfolio_id=portfolio_id,
                symbol=symbol,
                name=symbol,
                sector=None,
                shares=0.0,
                avg_cost_basis=avg_cost,
                target_weight=0.0,
            )
            db.add(pos)
            await db.flush()

        for lot_data in sym_lots:
            await lot_engine.open_lot(
                db=db,
                position_id=pos.id,
                shares=lot_data["shares"],
                cost_basis_per_share=lot_data["cost_per_share"],
                purchase_date=lot_data["date_acquired"],
            )
            imported += 1

        # Recalculate position-level summary from all open lots
        all_lots_result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == pos.id,
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            )
        )
        all_lots = all_lots_result.scalars().all()
        total_shares = sum(l.shares for l in all_lots)
        total_cost_val = sum(l.shares * l.cost_basis for l in all_lots)
        pos.shares = total_shares
        pos.avg_cost_basis = total_cost_val / total_shares if total_shares > 0 else 0.0

        symbols_touched.add(symbol)

    await db.commit()

    return {
        "imported_lots": imported,
        "skipped": skipped,
        "symbols": sorted(symbols_touched),
        "symbol_count": len(symbols_touched),
    }
