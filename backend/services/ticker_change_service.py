"""
Ticker change automation.

Handles symbol renames and class conversions: FB → META, GOOG → GOOGL C-shares,
TWTR → X (delisted in practice, but same pattern applies). Share count, cost
basis, purchase date, and wash-sale state are preserved — only the symbol
string changes. Tax lots follow their parent Position via FK so they don't
need individual updates.

Idempotency: each rename is recorded as a `CorporateActionLog` row with
`action_type="ticker_change"`, `symbol=<new>`, `notes=<old_symbol>`, and
`ex_date=<effective_date>`. The same `(new_symbol, ex_date)` pair will not be
applied twice.

Collision handling: if the target portfolio already has a Position for the
new symbol (e.g. the user held both META and the legacy FB — edge case, but
possible if two lots were imported from different CSVs), we merge them:
lots are re-parented to the target Position, its `shares` and `avg_cost_basis`
are recomputed from the union, and the old Position is marked inactive. The
lot engine treats the merged lots as independent for ST/LT and wash-sale
purposes because `purchase_date` is per-lot, not per-position.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.models import CorporateActionLog, Position, TaxLot


async def _already_processed(
    db: AsyncSession, new_symbol: str, ex_date: datetime | None,
) -> bool:
    stmt = select(CorporateActionLog).where(
        CorporateActionLog.symbol == new_symbol,
        CorporateActionLog.action_type == "ticker_change",
    )
    if ex_date:
        stmt = stmt.where(CorporateActionLog.ex_date == ex_date)
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def process_ticker_change(
    db: AsyncSession,
    old_symbol: str,
    new_symbol: str,
    ex_date: datetime | None = None,
    notes: str | None = None,
) -> dict:
    """
    Rename every active Position.symbol from old_symbol to new_symbol.

    If a portfolio already has a Position in new_symbol, the old Position's
    lots are re-parented and share/basis fields are merged into the existing
    target Position. Otherwise only the `symbol` / `name` fields change.

    Returns a summary dict with `positions_renamed`, `positions_merged`,
    `lots_migrated`, and the canonical log fields.
    """
    old_symbol = old_symbol.strip().upper()
    new_symbol = new_symbol.strip().upper()
    if not old_symbol or not new_symbol:
        raise ValueError("old_symbol and new_symbol are both required")
    if old_symbol == new_symbol:
        raise ValueError("old_symbol and new_symbol must differ")

    ex_date = ex_date or datetime.utcnow()

    if await _already_processed(db, new_symbol, ex_date):
        return {
            "skipped": True,
            "reason": "already processed",
            "old_symbol": old_symbol,
            "new_symbol": new_symbol,
            "ex_date": ex_date.isoformat(),
        }

    # All active Positions holding the old symbol across every portfolio
    old_positions_result = await db.execute(
        select(Position).where(
            Position.symbol == old_symbol,
            Position.is_active == True,  # noqa: E712
        )
    )
    old_positions = old_positions_result.scalars().all()

    positions_renamed = 0
    positions_merged = 0
    lots_migrated = 0

    for old_pos in old_positions:
        # Is there already a target Position (new_symbol) in the same portfolio?
        target_result = await db.execute(
            select(Position).where(
                Position.portfolio_id == old_pos.portfolio_id,
                Position.symbol == new_symbol,
                Position.is_active == True,  # noqa: E712
            )
        )
        target = target_result.scalar_one_or_none()

        if target is None:
            # Simple rename — no collision. Lots stay attached by FK.
            old_pos.symbol = new_symbol
            # Nudge the display name only if it was just the old symbol
            if old_pos.name == old_symbol:
                old_pos.name = new_symbol
            positions_renamed += 1
            continue

        # Collision: merge old_pos into target. Re-parent open lots, recompute
        # target's summary fields, deactivate the old Position.
        lots_r = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == old_pos.id,
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            )
        )
        old_lots = lots_r.scalars().all()

        total_old_cost = sum(l.shares * l.cost_basis for l in old_lots)
        total_old_shares = sum(l.shares for l in old_lots)

        for lot in old_lots:
            lot.position_id = target.id
            lots_migrated += 1

        total_target_cost = target.avg_cost_basis * target.shares
        new_total_shares = target.shares + total_old_shares
        if new_total_shares > 0:
            target.avg_cost_basis = (total_target_cost + total_old_cost) / new_total_shares
        target.shares = round(new_total_shares, 6)

        old_pos.shares = 0.0
        old_pos.is_active = False
        positions_merged += 1

    log_notes_body = notes or f"Ticker change {old_symbol} -> {new_symbol}"
    db.add(CorporateActionLog(
        symbol=new_symbol,
        action_type="ticker_change",
        old_rate=None,
        new_rate=None,
        ratio=1.0,
        ex_date=ex_date,
        positions_affected=positions_renamed + positions_merged,
        notes=f"{log_notes_body} | old_symbol={old_symbol}",
    ))
    await db.commit()

    return {
        "old_symbol": old_symbol,
        "new_symbol": new_symbol,
        "ex_date": ex_date.isoformat(),
        "positions_renamed": positions_renamed,
        "positions_merged": positions_merged,
        "lots_migrated": lots_migrated,
        "positions_affected": positions_renamed + positions_merged,
    }
