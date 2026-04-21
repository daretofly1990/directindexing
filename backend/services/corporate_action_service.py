"""
Corporate action processing.

Supported actions:
  split         — multiply shares by ratio, divide cost_basis by ratio
  reverse_split — divide shares by ratio, multiply cost_basis by ratio

Both actions are applied to:
  - Position.shares and Position.avg_cost_basis
  - All open TaxLot.shares and TaxLot.cost_basis for that position

A CorporateActionLog row is written so the same action is never applied twice.
"""
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Position, TaxLot, CorporateActionLog


async def _already_processed(db: AsyncSession, symbol: str, ex_date: datetime | None) -> bool:
    stmt = select(CorporateActionLog).where(CorporateActionLog.symbol == symbol)
    if ex_date:
        stmt = stmt.where(CorporateActionLog.ex_date == ex_date)
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def process_split(
    db: AsyncSession,
    symbol: str,
    old_rate: float,
    new_rate: float,
    ex_date: datetime | None = None,
    notes: str | None = None,
) -> dict:
    """
    Apply a split or reverse-split to all active positions in the symbol.

    ratio = new_rate / old_rate
      > 1.0 → forward split  (shares increase, cost basis decreases)
      < 1.0 → reverse split  (shares decrease, cost basis increases)
    """
    if old_rate <= 0 or new_rate <= 0:
        raise ValueError("Rates must be positive.")

    ratio = new_rate / old_rate
    action_type = "split" if ratio > 1.0 else "reverse_split"

    if ex_date and await _already_processed(db, symbol, ex_date):
        return {"skipped": True, "reason": "already processed", "symbol": symbol}

    pos_result = await db.execute(
        select(Position).where(
            Position.symbol == symbol,
            Position.is_active == True,  # noqa: E712
        )
    )
    positions = pos_result.scalars().all()
    positions_affected = 0

    for pos in positions:
        # Adjust position-level summary fields
        pos.shares = round(pos.shares * ratio, 6)
        pos.avg_cost_basis = pos.avg_cost_basis / ratio

        # Adjust every open lot
        lot_result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == pos.id,
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            )
        )
        for lot in lot_result.scalars().all():
            lot.shares = round(lot.shares * ratio, 6)
            lot.cost_basis = lot.cost_basis / ratio

        positions_affected += 1

    log = CorporateActionLog(
        symbol=symbol,
        action_type=action_type,
        old_rate=old_rate,
        new_rate=new_rate,
        ratio=ratio,
        ex_date=ex_date,
        positions_affected=positions_affected,
        notes=notes,
    )
    db.add(log)
    await db.commit()

    return {
        "symbol": symbol,
        "action_type": action_type,
        "ratio": ratio,
        "positions_affected": positions_affected,
        "ex_date": ex_date.isoformat() if ex_date else None,
    }


async def process_delisting(
    db: AsyncSession,
    symbol: str,
    ex_date: datetime | None = None,
    notes: str | None = None,
) -> dict:
    """
    Mark a symbol delisted across every portfolio. Position.is_delisted=True and
    delisted_at=ex_date. Tax lots are NOT auto-closed — the customer decides
    whether to write off or wait for broker cleanup. TLH scans skip delisted
    positions.
    """
    ex_date = ex_date or datetime.utcnow()
    if await _already_processed(db, symbol, ex_date):
        return {"skipped": True, "reason": "already processed", "symbol": symbol}

    pos_result = await db.execute(
        select(Position).where(
            Position.symbol == symbol,
            Position.is_delisted == False,  # noqa: E712
        )
    )
    positions = pos_result.scalars().all()
    count = 0
    for pos in positions:
        pos.is_delisted = True
        pos.delisted_at = ex_date
        count += 1

    db.add(CorporateActionLog(
        symbol=symbol, action_type="delisting",
        old_rate=None, new_rate=None, ratio=1.0,
        ex_date=ex_date, positions_affected=count, notes=notes,
    ))
    await db.commit()
    return {"symbol": symbol, "action_type": "delisting", "positions_affected": count,
            "ex_date": ex_date.isoformat()}


async def process_spinoff(
    db: AsyncSession,
    parent_symbol: str,
    spin_symbol: str,
    shares_per_parent: float,
    basis_allocation_parent_pct: float,
    ex_date: datetime | None = None,
    notes: str | None = None,
) -> dict:
    """
    Spin-off: for every open lot of `parent_symbol`, a new open lot of
    `spin_symbol` is created holding `shares_per_parent * parent_lot_shares`
    shares. Basis is allocated: parent retains `basis_allocation_parent_pct`,
    spin takes the remainder. Purchase date carries over (IRS rule).

    Example: ABC spins off XYZ at 0.4 shares XYZ per share of ABC, with basis
    allocation 80% parent / 20% spin. If you hold 100 ABC @ $50 basis ($5000
    total), post-spin you hold 100 ABC @ $40 basis ($4000) and 40 XYZ @ $25
    basis ($1000). Purchase date is preserved so ST/LT treatment is unchanged.
    """
    if not (0 < basis_allocation_parent_pct <= 1):
        raise ValueError("basis_allocation_parent_pct must be in (0, 1]")
    if shares_per_parent <= 0:
        raise ValueError("shares_per_parent must be > 0")

    ex_date = ex_date or datetime.utcnow()
    if await _already_processed(db, parent_symbol, ex_date):
        return {"skipped": True, "reason": "already processed", "symbol": parent_symbol}

    parent_pos_result = await db.execute(
        select(Position).where(
            Position.symbol == parent_symbol,
            Position.is_active == True,  # noqa: E712
        )
    )
    parents = parent_pos_result.scalars().all()
    affected = 0

    for parent in parents:
        # Reduce parent cost basis
        new_parent_basis = parent.avg_cost_basis * basis_allocation_parent_pct
        parent.avg_cost_basis = new_parent_basis

        # Open lots to reshape
        lot_result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == parent.id,
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            )
        )
        parent_lots = lot_result.scalars().all()
        if not parent_lots:
            continue

        # Find or create the spin position in this portfolio
        spin_pos_result = await db.execute(
            select(Position).where(
                Position.portfolio_id == parent.portfolio_id,
                Position.symbol == spin_symbol,
            )
        )
        spin_pos = spin_pos_result.scalar_one_or_none()
        if spin_pos is None:
            spin_pos = Position(
                portfolio_id=parent.portfolio_id,
                symbol=spin_symbol,
                name=f"{spin_symbol} (spun off from {parent_symbol})",
                sector=parent.sector,
                shares=0.0,
                avg_cost_basis=0.0,
                target_weight=0.0,
                is_active=True,
            )
            db.add(spin_pos)
            await db.flush()

        total_spin_shares = 0.0
        total_spin_cost = 0.0
        for lot in parent_lots:
            # Reduce parent lot basis
            spin_lot_shares = lot.shares * shares_per_parent
            original_parent_lot_basis = lot.cost_basis
            lot.cost_basis = original_parent_lot_basis * basis_allocation_parent_pct

            # Create corresponding spin lot with preserved purchase_date
            spin_per_share_basis = (
                (original_parent_lot_basis * (1 - basis_allocation_parent_pct) * lot.shares)
                / spin_lot_shares
            ) if spin_lot_shares > 0 else 0.0
            db.add(TaxLot(
                position_id=spin_pos.id,
                shares=spin_lot_shares,
                cost_basis=spin_per_share_basis,
                purchase_date=lot.purchase_date,
                wash_sale_disallowed=0.0,
            ))
            total_spin_shares += spin_lot_shares
            total_spin_cost += spin_per_share_basis * spin_lot_shares

        # Update summary fields on spin position
        spin_pos.shares = (spin_pos.shares or 0) + total_spin_shares
        if spin_pos.shares > 0:
            spin_pos.avg_cost_basis = (
                (spin_pos.avg_cost_basis * (spin_pos.shares - total_spin_shares) + total_spin_cost)
                / spin_pos.shares
            )
        affected += 1

    db.add(CorporateActionLog(
        symbol=parent_symbol, action_type="spinoff",
        old_rate=None, new_rate=shares_per_parent,
        ratio=shares_per_parent,
        ex_date=ex_date, positions_affected=affected,
        notes=notes or f"Spin {spin_symbol} @ {shares_per_parent}/share; "
                      f"basis {basis_allocation_parent_pct:.0%} parent",
    ))
    await db.commit()
    return {
        "parent_symbol": parent_symbol,
        "spin_symbol": spin_symbol,
        "shares_per_parent": shares_per_parent,
        "basis_allocation_parent_pct": basis_allocation_parent_pct,
        "positions_affected": affected,
        "ex_date": ex_date.isoformat(),
    }


async def process_merger_cash(
    db: AsyncSession,
    symbol: str,
    cash_per_share: float,
    ex_date: datetime | None = None,
    notes: str | None = None,
) -> dict:
    """
    All-cash merger: every open lot is closed at cash_per_share, position
    deactivated, realized gain/loss recorded. This is a taxable event.
    """
    if cash_per_share <= 0:
        raise ValueError("cash_per_share must be > 0")
    ex_date = ex_date or datetime.utcnow()
    if await _already_processed(db, symbol, ex_date):
        return {"skipped": True, "reason": "already processed", "symbol": symbol}

    from .lot_engine import lot_engine

    pos_result = await db.execute(
        select(Position).where(
            Position.symbol == symbol,
            Position.is_active == True,  # noqa: E712
        )
    )
    positions = pos_result.scalars().all()
    affected = 0
    total_proceeds = 0.0
    total_gain = 0.0

    for pos in positions:
        lots_r = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == pos.id,
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            )
        )
        open_lot_ids = [l.id for l in lots_r.scalars().all()]
        if not open_lot_ids:
            continue
        result = await lot_engine.close_lots_by_ids(
            db=db, lot_ids=open_lot_ids,
            sale_price=cash_per_share, sale_date=ex_date,
        )
        pos.shares = 0.0
        pos.is_active = False
        affected += 1
        total_proceeds += sum(d["proceeds"] for d in result["closed_lots"])
        total_gain += result["total_gain"]

    db.add(CorporateActionLog(
        symbol=symbol, action_type="merger_cash",
        old_rate=None, new_rate=cash_per_share, ratio=1.0,
        ex_date=ex_date, positions_affected=affected,
        notes=notes or f"All-cash merger @ ${cash_per_share}/share",
    ))
    await db.commit()
    return {
        "symbol": symbol, "action_type": "merger_cash",
        "cash_per_share": cash_per_share,
        "positions_affected": affected,
        "total_proceeds": total_proceeds,
        "total_gain_loss": total_gain,
        "ex_date": ex_date.isoformat(),
    }
