"""Tests for split/reverse-split processing and idempotency."""
import pytest
from datetime import datetime

from backend.models.models import Portfolio, Position
from backend.services.lot_engine import lot_engine
from backend.services.corporate_action_service import process_split


async def _make_position(db, symbol="AAPL", shares=100.0, avg_cost=150.0):
    port = Portfolio(name="CA Test", initial_value=100_000, cash=100_000)
    db.add(port)
    await db.commit()
    await db.refresh(port)

    pos = Position(
        portfolio_id=port.id,
        symbol=symbol,
        name=symbol,
        sector="Technology",
        shares=shares,
        avg_cost_basis=avg_cost,
        target_weight=1.0,
    )
    db.add(pos)
    await db.commit()
    await db.refresh(pos)
    await lot_engine.open_lot(db, pos.id, shares, avg_cost, datetime(2024, 1, 1))
    return port, pos


@pytest.mark.asyncio
async def test_forward_split(db):
    """2-for-1 split: shares double, cost basis halves."""
    _, pos = await _make_position(db, shares=100, avg_cost=200.0)
    result = await process_split(db, "AAPL", old_rate=1, new_rate=2)
    assert result["action_type"] == "split"
    assert result["positions_affected"] == 1

    await db.refresh(pos)
    assert pos.shares == pytest.approx(200.0)
    assert pos.avg_cost_basis == pytest.approx(100.0)

    lots = await lot_engine.get_open_lot_detail(db, pos.portfolio_id)
    assert lots[0]["shares"] == pytest.approx(200.0)
    assert lots[0]["cost_basis_per_share"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_reverse_split(db):
    """1-for-10 reverse split: shares divide by 10, cost basis multiplies by 10."""
    _, pos = await _make_position(db, shares=100, avg_cost=10.0)
    result = await process_split(db, "AAPL", old_rate=10, new_rate=1)
    assert result["action_type"] == "reverse_split"

    await db.refresh(pos)
    assert pos.shares == pytest.approx(10.0)
    assert pos.avg_cost_basis == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_split_idempotent_with_ex_date(db):
    """Same split with same ex_date should not be applied twice."""
    _, pos = await _make_position(db, shares=100, avg_cost=200.0)
    ex = datetime(2024, 6, 1)
    r1 = await process_split(db, "AAPL", 1, 2, ex_date=ex)
    r2 = await process_split(db, "AAPL", 1, 2, ex_date=ex)
    assert r1.get("skipped") is None
    assert r2.get("skipped") is True

    await db.refresh(pos)
    assert pos.shares == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_invalid_rates_raise(db):
    with pytest.raises(ValueError):
        await process_split(db, "AAPL", old_rate=0, new_rate=2)
    with pytest.raises(ValueError):
        await process_split(db, "AAPL", old_rate=1, new_rate=-1)
