"""Tests for the spin-off, cash-merger, and delisting corporate actions."""
from datetime import datetime

import pytest

from backend.models.models import Portfolio, Position, TaxLot
from backend.services.corporate_action_service import (
    process_spinoff, process_merger_cash, process_delisting,
)
from backend.services.lot_engine import lot_engine


async def _setup(db, symbol="ABC", shares=100, basis=50):
    port = Portfolio(name="T", initial_value=10_000, cash=0)
    db.add(port)
    await db.commit()
    await db.refresh(port)
    pos = Position(
        portfolio_id=port.id, symbol=symbol, name=symbol,
        sector="Tech", shares=shares, avg_cost_basis=basis, target_weight=1.0,
    )
    db.add(pos)
    await db.commit()
    await db.refresh(pos)
    await lot_engine.open_lot(db, pos.id, shares, basis, datetime(2023, 1, 1))
    await db.commit()
    return port, pos


@pytest.mark.asyncio
async def test_spinoff_creates_child_lot_with_preserved_date(db):
    port, parent = await _setup(db, "ABC", 100, 50)
    r = await process_spinoff(
        db,
        parent_symbol="ABC", spin_symbol="XYZ",
        shares_per_parent=0.4,
        basis_allocation_parent_pct=0.8,
    )
    assert r["positions_affected"] == 1
    from sqlalchemy import select
    spin_pos = (await db.execute(select(Position).where(Position.symbol == "XYZ"))).scalar_one()
    assert spin_pos.shares == pytest.approx(40.0)
    spin_lots = (await db.execute(
        select(TaxLot).where(TaxLot.position_id == spin_pos.id)
    )).scalars().all()
    assert len(spin_lots) == 1
    assert spin_lots[0].purchase_date == datetime(2023, 1, 1)
    # 20% of $5000 = $1000 basis / 40 shares = $25/share
    assert spin_lots[0].cost_basis == pytest.approx(25.0)
    # Parent basis reduced to 80% of original
    parent_lot = (await db.execute(
        select(TaxLot).where(TaxLot.position_id == parent.id)
    )).scalar_one()
    assert parent_lot.cost_basis == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_merger_cash_closes_position(db):
    port, pos = await _setup(db, "ABC", 100, 50)
    r = await process_merger_cash(db, "ABC", cash_per_share=70.0)
    assert r["positions_affected"] == 1
    assert r["total_proceeds"] == pytest.approx(7000.0)
    assert r["total_gain_loss"] == pytest.approx(2000.0)  # (70-50)*100
    # Position deactivated
    from sqlalchemy import select
    pos_r = (await db.execute(select(Position).where(Position.id == pos.id))).scalar_one()
    assert pos_r.is_active is False
    assert pos_r.shares == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_delisting_flags_position(db):
    port, pos = await _setup(db, "ABC", 10, 100)
    r = await process_delisting(db, "ABC")
    assert r["positions_affected"] == 1
    from sqlalchemy import select
    pos_r = (await db.execute(select(Position).where(Position.id == pos.id))).scalar_one()
    assert pos_r.is_delisted is True
    assert pos_r.delisted_at is not None
