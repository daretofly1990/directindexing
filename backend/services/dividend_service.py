"""
Dividend tracking: pull ex-dividend records from Finnhub, credit portfolios.

For each active position, fetch dividends with ex-date after the last processed
date. For each dividend: shares × amount is credited to the portfolio cash and
logged as a Transaction with type="DIVIDEND". Idempotent — the same (portfolio,
symbol, ex_date) dividend is never applied twice (marker stored in `notes`).
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Portfolio, Position, Transaction
from .audit import log_audit
from .finnhub_client import finnhub_client

logger = logging.getLogger(__name__)

# Encode ex-date in the Transaction notes so we can detect duplicates
NOTES_PREFIX = "DIVIDEND ex="  # followed by YYYY-MM-DD


def _notes_key(ex_date_str: str) -> str:
    return f"{NOTES_PREFIX}{ex_date_str}"


async def _already_applied(
    db: AsyncSession, portfolio_id: int, symbol: str, ex_date_str: str
) -> bool:
    marker = _notes_key(ex_date_str)
    result = await db.execute(
        select(Transaction.id).where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.symbol == symbol,
            Transaction.transaction_type == "DIVIDEND",
            Transaction.notes.like(f"%{marker}%"),
        )
    )
    return result.first() is not None


async def process_dividends_for_portfolio(
    db: AsyncSession,
    portfolio_id: int,
    lookback_days: int = 7,
) -> dict:
    """
    Fetch dividends for each active position in this portfolio, credit cash,
    record transactions. Dividends with ex-date earlier than today - lookback_days
    are skipped (catches recent payouts, avoids full history scan).
    """
    portfolio = await db.get(Portfolio, portfolio_id)
    if not portfolio:
        raise ValueError("Portfolio not found")

    result = await db.execute(
        select(Position).where(
            Position.portfolio_id == portfolio_id,
            Position.is_active == True,  # noqa: E712
            Position.shares > 0,
        )
    )
    positions = result.scalars().all()

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=lookback_days)).isoformat()
    to_date = today.isoformat()

    applied = []
    total_cash_credited = 0.0

    for pos in positions:
        try:
            dividends = await finnhub_client.get_dividends(pos.symbol, from_date, to_date)
        except Exception as exc:
            logger.warning("Dividend fetch failed for %s: %s", pos.symbol, exc)
            continue

        for d in dividends:
            ex_date_str = d.get("date") or ""
            amount = d.get("amount") or d.get("adjustedAmount") or 0
            if not ex_date_str or not amount:
                continue
            if await _already_applied(db, portfolio_id, pos.symbol, ex_date_str):
                continue

            try:
                ex_dt = datetime.fromisoformat(ex_date_str)
            except ValueError:
                continue

            total_value = pos.shares * float(amount)
            txn = Transaction(
                portfolio_id=portfolio_id,
                symbol=pos.symbol,
                transaction_type="DIVIDEND",
                shares=pos.shares,
                price=float(amount),
                total_value=total_value,
                timestamp=ex_dt,
                notes=f"{_notes_key(ex_date_str)} ${amount:.4f}/share",
            )
            db.add(txn)
            portfolio.cash += total_value
            total_cash_credited += total_value
            applied.append({
                "symbol": pos.symbol,
                "ex_date": ex_date_str,
                "amount_per_share": float(amount),
                "shares": pos.shares,
                "total": total_value,
            })

    if applied:
        await log_audit(
            db, event_type="DIVIDEND_APPLIED",
            portfolio_id=portfolio_id,
            object_type="portfolio", object_id=portfolio_id,
            details={
                "count": len(applied),
                "total_cash_credited": total_cash_credited,
                "symbols": sorted({a["symbol"] for a in applied}),
            },
        )
    await db.commit()
    return {
        "portfolio_id": portfolio_id,
        "positions_checked": len(positions),
        "dividends_applied": len(applied),
        "total_cash_credited": total_cash_credited,
        "applied": applied,
        "from_date": from_date,
        "to_date": to_date,
    }


async def process_dividends_all_portfolios(db: AsyncSession, lookback_days: int = 7) -> dict:
    """Run dividend processing for every portfolio in the DB."""
    result = await db.execute(select(Portfolio.id))
    portfolio_ids = [row[0] for row in result.all()]

    summary = []
    total_applied = 0
    total_credited = 0.0
    for pid in portfolio_ids:
        try:
            r = await process_dividends_for_portfolio(db, pid, lookback_days=lookback_days)
            summary.append({
                "portfolio_id": pid,
                "dividends_applied": r["dividends_applied"],
                "total_cash_credited": r["total_cash_credited"],
            })
            total_applied += r["dividends_applied"]
            total_credited += r["total_cash_credited"]
        except Exception as exc:
            logger.error("Dividend processing failed for portfolio %s: %s", pid, exc)
            summary.append({"portfolio_id": pid, "error": str(exc)})

    return {
        "portfolios_processed": len(portfolio_ids),
        "total_dividends_applied": total_applied,
        "total_cash_credited": total_credited,
        "per_portfolio": summary,
    }
