"""Tests for the ticker-change corporate action."""
from datetime import datetime

import pytest
from sqlalchemy import select

from backend.models.models import Portfolio, Position, TaxLot
from backend.services.lot_engine import lot_engine
from backend.services.ticker_change_service import process_ticker_change


async def _setup(db, symbol="FB", shares=100.0, basis=200.0, purchase_date=None):
    port = Portfolio(name="TC Test", initial_value=50_000, cash=0)
    db.add(port)
    await db.commit()
    await db.refresh(port)

    pos = Position(
        portfolio_id=port.id,
        symbol=symbol,
        name=symbol,
        sector="Communication Services",
        shares=shares,
        avg_cost_basis=basis,
        target_weight=1.0,
    )
    db.add(pos)
    await db.commit()
    await db.refresh(pos)
    await lot_engine.open_lot(
        db, pos.id, shares, basis, purchase_date or datetime(2021, 6, 1),
    )
    await db.commit()
    return port, pos


@pytest.mark.asyncio
async def test_simple_rename_preserves_shares_and_basis(db):
    """FB -> META: cost basis, share count, and purchase date are unchanged."""
    _, pos = await _setup(db, "FB", shares=50, basis=300.0)
    r = await process_ticker_change(db, "FB", "META")
    assert r["positions_renamed"] == 1
    assert r["positions_merged"] == 0
    assert r["lots_migrated"] == 0

    await db.refresh(pos)
    assert pos.symbol == "META"
    assert pos.name == "META"
    assert pos.shares == pytest.approx(50.0)
    assert pos.avg_cost_basis == pytest.approx(300.0)

    # Lot purchase date and per-share basis survive
    lot = (
        await db.execute(select(TaxLot).where(TaxLot.position_id == pos.id))
    ).scalar_one()
    assert lot.cost_basis == pytest.approx(300.0)
    assert lot.purchase_date == datetime(2021, 6, 1)


@pytest.mark.asyncio
async def test_rename_is_idempotent_on_same_ex_date(db):
    """Running the same change twice with the same ex_date is a no-op."""
    _, pos = await _setup(db, "FB", shares=10, basis=200.0)
    ex = datetime(2022, 6, 9)
    r1 = await process_ticker_change(db, "FB", "META", ex_date=ex)
    r2 = await process_ticker_change(db, "FB", "META", ex_date=ex)

    assert r1.get("skipped") is None
    assert r2.get("skipped") is True
    await db.refresh(pos)
    assert pos.symbol == "META"


@pytest.mark.asyncio
async def test_collision_merges_lots_and_averages_basis(db):
    """
    Portfolio already holds META when FB renames. Old lots re-parent to META,
    weighted-average cost basis is recomputed, old Position is deactivated.
    """
    port, old_pos = await _setup(db, "FB", shares=100, basis=200.0)
    # Second position in the same portfolio, already in the new symbol
    target = Position(
        portfolio_id=port.id, symbol="META", name="META",
        sector="Communication Services", shares=50, avg_cost_basis=400.0,
        target_weight=0.5,
    )
    db.add(target)
    await db.commit()
    await db.refresh(target)
    await lot_engine.open_lot(db, target.id, 50, 400.0, datetime(2023, 1, 1))
    await db.commit()

    r = await process_ticker_change(db, "FB", "META")
    assert r["positions_merged"] == 1
    assert r["lots_migrated"] == 1

    await db.refresh(old_pos)
    await db.refresh(target)
    assert old_pos.is_active is False
    assert old_pos.shares == pytest.approx(0.0)

    # 100 * 200 + 50 * 400 = 20000 + 20000 = 40000, over 150 shares = $266.67
    assert target.shares == pytest.approx(150.0)
    assert target.avg_cost_basis == pytest.approx(40_000.0 / 150.0)

    # Both lots re-parented to target, both purchase dates intact
    target_lots = (
        await db.execute(select(TaxLot).where(TaxLot.position_id == target.id))
    ).scalars().all()
    assert len(target_lots) == 2
    dates = sorted(l.purchase_date for l in target_lots)
    assert dates == [datetime(2021, 6, 1), datetime(2023, 1, 1)]


@pytest.mark.asyncio
async def test_invalid_inputs_raise(db):
    with pytest.raises(ValueError):
        await process_ticker_change(db, "", "META")
    with pytest.raises(ValueError):
        await process_ticker_change(db, "FB", "")
    with pytest.raises(ValueError):
        await process_ticker_change(db, "FB", "FB")


@pytest.mark.asyncio
async def test_no_matching_positions_is_noop(db):
    """Running against a symbol no one holds completes with zero affected."""
    r = await process_ticker_change(db, "FB", "META")
    assert r["positions_affected"] == 0
    assert r["positions_renamed"] == 0
    assert r["positions_merged"] == 0
