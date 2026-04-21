"""Tests for dividend processing: credits cash, is idempotent."""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend.models.models import Portfolio, Position, Transaction
from backend.services import dividend_service


async def _setup(db):
    port = Portfolio(name="T", initial_value=10_000, cash=0.0)
    db.add(port)
    await db.commit()
    await db.refresh(port)
    pos = Position(
        portfolio_id=port.id, symbol="AAPL", name="Apple",
        sector="Technology", shares=100.0, avg_cost_basis=150.0, target_weight=1.0,
    )
    db.add(pos)
    await db.commit()
    await db.refresh(pos)
    return port, pos


@pytest.mark.asyncio
async def test_dividend_credits_cash_and_records_transaction(db):
    port, pos = await _setup(db)
    fake_divs = [{"date": "2026-04-10", "amount": 0.25, "symbol": "AAPL"}]

    async def fake_get(symbol, from_date, to_date):
        return fake_divs

    with patch.object(dividend_service.finnhub_client, "get_dividends", side_effect=fake_get):
        r = await dividend_service.process_dividends_for_portfolio(db, port.id, lookback_days=30)

    assert r["dividends_applied"] == 1
    assert r["total_cash_credited"] == pytest.approx(25.0)  # 100 shares * 0.25

    port_r = await db.get(Portfolio, port.id)
    assert port_r.cash == pytest.approx(25.0)

    txns = (await db.execute(select(Transaction).where(Transaction.portfolio_id == port.id))).scalars().all()
    assert len(txns) == 1
    assert txns[0].transaction_type == "DIVIDEND"
    assert txns[0].total_value == pytest.approx(25.0)
    assert "ex=2026-04-10" in txns[0].notes


@pytest.mark.asyncio
async def test_dividend_is_idempotent(db):
    port, pos = await _setup(db)
    fake_divs = [{"date": "2026-04-10", "amount": 0.25, "symbol": "AAPL"}]

    async def fake_get(symbol, from_date, to_date):
        return fake_divs

    with patch.object(dividend_service.finnhub_client, "get_dividends", side_effect=fake_get):
        r1 = await dividend_service.process_dividends_for_portfolio(db, port.id, lookback_days=30)
        r2 = await dividend_service.process_dividends_for_portfolio(db, port.id, lookback_days=30)

    assert r1["dividends_applied"] == 1
    assert r2["dividends_applied"] == 0  # second run is a no-op
    port_r = await db.get(Portfolio, port.id)
    assert port_r.cash == pytest.approx(25.0)  # not doubled


@pytest.mark.asyncio
async def test_dividend_skips_inactive_position(db):
    port, pos = await _setup(db)
    pos.is_active = False
    await db.commit()

    async def fake_get(symbol, from_date, to_date):
        return [{"date": "2026-04-10", "amount": 0.25, "symbol": "AAPL"}]

    with patch.object(dividend_service.finnhub_client, "get_dividends", side_effect=fake_get):
        r = await dividend_service.process_dividends_for_portfolio(db, port.id, lookback_days=30)

    assert r["positions_checked"] == 0
    assert r["dividends_applied"] == 0
