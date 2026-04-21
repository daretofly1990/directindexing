"""
Household-scoped wash-sale: for a given Portfolio (belonging to a Client that
may be grouped in a Household), return the BUY transactions across all
sibling portfolios in the last N days that would trigger a wash-sale.

Why: IRS rule 1091 treats substantially-identical purchases across a taxpayer's
household (including IRAs and spouse accounts if filing jointly) as
disallowing the loss. Account-level scoping misses these cross-account triggers.
"""
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Portfolio, Client, Transaction


async def household_portfolio_ids(db: AsyncSession, portfolio_id: int) -> list[int]:
    """Return this portfolio's id plus any sibling portfolios under the same household."""
    port = await db.get(Portfolio, portfolio_id)
    if not port or port.client_id is None:
        return [portfolio_id]
    client = await db.get(Client, port.client_id)
    if not client or client.household_id is None:
        return [portfolio_id]
    # Every client in this household
    r = await db.execute(select(Client.id).where(Client.household_id == client.household_id))
    client_ids = [row[0] for row in r.all()]
    # Every portfolio owned by those clients
    r2 = await db.execute(select(Portfolio.id).where(Portfolio.client_id.in_(client_ids)))
    return [row[0] for row in r2.all()] or [portfolio_id]


async def household_recent_buys(
    db: AsyncSession, portfolio_id: int, symbol: str, window_days: int = 30,
) -> list[Transaction]:
    """
    Every BUY of `symbol` across the household in the last `window_days`.
    An empty list means no household-level wash-sale trigger.
    """
    ids = await household_portfolio_ids(db, portfolio_id)
    window_start = datetime.utcnow() - timedelta(days=window_days)
    r = await db.execute(
        select(Transaction).where(
            Transaction.portfolio_id.in_(ids),
            Transaction.symbol == symbol,
            Transaction.transaction_type == "BUY",
            Transaction.timestamp >= window_start,
        )
    )
    return r.scalars().all()
