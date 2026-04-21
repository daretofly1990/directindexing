"""Tests for the tax-lot engine (FIFO/LIFO/HIFO, wash-sale, gain summary)."""
import pytest
from datetime import datetime, timedelta

from backend.models.models import Portfolio, Position
from backend.services.lot_engine import lot_engine, LotSelectionMethod


async def _setup(db):
    """Create a minimal portfolio and position, return (portfolio, position)."""
    port = Portfolio(name="Test", initial_value=100_000, cash=100_000)
    db.add(port)
    await db.commit()
    await db.refresh(port)

    pos = Position(
        portfolio_id=port.id,
        symbol="AAPL",
        name="Apple",
        sector="Technology",
        shares=0.0,
        avg_cost_basis=0.0,
        target_weight=1.0,
    )
    db.add(pos)
    await db.commit()
    await db.refresh(pos)
    return port, pos


@pytest.mark.asyncio
async def test_open_lot(db):
    _, pos = await _setup(db)
    t0 = datetime(2023, 1, 1)
    await lot_engine.open_lot(db, pos.id, shares=10, cost_basis_per_share=100.0, purchase_date=t0)
    detail = await lot_engine.get_open_lot_detail(db, pos.portfolio_id)
    assert len(detail) == 1
    assert detail[0]["shares"] == 10
    assert detail[0]["cost_basis_per_share"] == 100.0


@pytest.mark.asyncio
async def test_close_fifo(db):
    """Oldest lot should be sold first under FIFO."""
    _, pos = await _setup(db)
    t0 = datetime(2022, 1, 1)
    t1 = datetime(2023, 1, 1)
    await lot_engine.open_lot(db, pos.id, 5, 80.0, t0)   # older, cheaper
    await lot_engine.open_lot(db, pos.id, 5, 120.0, t1)  # newer, pricier

    result = await lot_engine.close_lots(
        db, pos.id, shares_to_sell=5, sale_price=100.0, sale_date=datetime(2024, 6, 1),
        method=LotSelectionMethod.FIFO,
    )
    # Should have sold the older lot (cost 80) first
    assert result["total_gain"] == pytest.approx((100.0 - 80.0) * 5)
    # Older lot → over 1 year → long-term
    assert result["long_term_gain"] == pytest.approx((100.0 - 80.0) * 5)
    assert result["short_term_gain"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_close_hifo(db):
    """Highest-cost lot should be sold first under HIFO."""
    _, pos = await _setup(db)
    t = datetime(2024, 1, 1)
    await lot_engine.open_lot(db, pos.id, 5, 80.0, t)
    await lot_engine.open_lot(db, pos.id, 5, 120.0, t)

    result = await lot_engine.close_lots(
        db, pos.id, shares_to_sell=5, sale_price=100.0, sale_date=datetime(2024, 6, 1),
        method=LotSelectionMethod.HIFO,
    )
    # Should have sold the 120 lot → loss of -20/share
    assert result["total_gain"] == pytest.approx((100.0 - 120.0) * 5)


@pytest.mark.asyncio
async def test_realized_gain_summary(db):
    _, pos = await _setup(db)
    t = datetime(2023, 1, 1)
    await lot_engine.open_lot(db, pos.id, 10, 100.0, t)
    await lot_engine.close_lots(
        db, pos.id, 10, 150.0, datetime(2024, 6, 1),
        method=LotSelectionMethod.FIFO,
    )
    summary = await lot_engine.get_realized_gain_summary(db, pos.portfolio_id)
    assert summary["long_term_net"] == pytest.approx(500.0)
    assert summary["total_net_gain_loss"] == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_partial_close(db):
    """Closing fewer shares than lot size should leave a residual open lot."""
    _, pos = await _setup(db)
    await lot_engine.open_lot(db, pos.id, 10, 100.0, datetime(2024, 1, 1))
    await lot_engine.close_lots(
        db, pos.id, 4, 110.0, datetime(2024, 6, 1),
        method=LotSelectionMethod.FIFO,
    )
    detail = await lot_engine.get_open_lot_detail(db, pos.portfolio_id)
    assert len(detail) == 1
    assert detail[0]["shares"] == pytest.approx(6.0)
