"""Tests for Spec-ID sale execution."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.models.models import Portfolio, Position, TaxLot, Transaction
from backend.services import ai_guardrails, kill_switch, sell_service
from backend.services.lot_engine import lot_engine
from backend.services.sell_service import execute_spec_id_sale


@pytest.fixture(autouse=True)
def _mock_finnhub_and_reset_killswitch(monkeypatch):
    """
    Keep these tests offline-stable:
      - mock live quotes at $1000 so NAV is always large enough that the new
        30%-of-NAV sell cap doesn't accidentally block test sales
      - reset the kill-switch cache between tests
    """
    async def fake_quotes(symbols):
        return {s: {"current_price": 1000.0} for s in symbols}
    async def fake_quote(symbol):
        return {"current_price": 1000.0}
    monkeypatch.setattr(ai_guardrails.finnhub_client, "get_multiple_quotes", fake_quotes)
    monkeypatch.setattr(sell_service.finnhub_client, "get_quote", fake_quote)
    kill_switch._cache["ts"] = 0.0


async def _setup_portfolio_with_lots(db):
    port = Portfolio(name="T", initial_value=100_000, cash=0)
    db.add(port)
    await db.commit()
    await db.refresh(port)

    pos = Position(
        portfolio_id=port.id, symbol="MSFT", name="Microsoft",
        sector="Technology", shares=30.0, avg_cost_basis=100.0, target_weight=1.0,
    )
    db.add(pos)
    await db.commit()
    await db.refresh(pos)

    t_old = datetime.utcnow() - timedelta(days=400)  # long-term
    t_new = datetime.utcnow() - timedelta(days=30)   # short-term
    await lot_engine.open_lot(db, pos.id, 10, 80.0, t_old)
    await lot_engine.open_lot(db, pos.id, 10, 100.0, t_new)
    await lot_engine.open_lot(db, pos.id, 10, 130.0, t_new)
    await db.commit()

    return port, pos


@pytest.mark.asyncio
async def test_spec_id_sell_single_lot_loss(db):
    port, pos = await _setup_portfolio_with_lots(db)
    # find the 130-cost lot
    lots = (await db.execute(select(TaxLot).where(TaxLot.position_id == pos.id))).scalars().all()
    hi_lot = next(l for l in lots if l.cost_basis == 130.0)

    r = await execute_spec_id_sale(
        db, portfolio_id=port.id, position_id=pos.id,
        lot_ids=[hi_lot.id], override_price=100.0,
    )

    assert r["shares_sold"] == pytest.approx(10.0)
    assert r["proceeds"] == pytest.approx(1000.0)
    assert r["economic_gain_loss"] == pytest.approx(-300.0)  # short-term loss
    assert r["short_term_gain_loss"] == pytest.approx(-300.0)
    assert r["long_term_gain_loss"] == pytest.approx(0.0)
    assert r["remaining_shares"] == pytest.approx(20.0)
    assert r["position_active"] is True

    # Cash credited
    port_refresh = await db.get(Portfolio, port.id)
    assert port_refresh.cash == pytest.approx(1000.0)

    # Transaction recorded
    txns = (await db.execute(select(Transaction).where(Transaction.portfolio_id == port.id))).scalars().all()
    assert len(txns) == 1
    assert txns[0].transaction_type == "SELL"
    assert txns[0].shares == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_spec_id_sell_deactivates_position_when_fully_sold(db):
    port, pos = await _setup_portfolio_with_lots(db)
    lots = (await db.execute(select(TaxLot).where(TaxLot.position_id == pos.id))).scalars().all()
    lot_ids = [l.id for l in lots]

    r = await execute_spec_id_sale(
        db, portfolio_id=port.id, position_id=pos.id,
        lot_ids=lot_ids, override_price=110.0,
    )

    assert r["shares_sold"] == pytest.approx(30.0)
    assert r["remaining_shares"] == pytest.approx(0.0)
    assert r["position_active"] is False


@pytest.mark.asyncio
async def test_spec_id_sell_rejects_lot_from_wrong_position(db):
    port, pos = await _setup_portfolio_with_lots(db)
    # create a second position with its own lot
    pos2 = Position(
        portfolio_id=port.id, symbol="AAPL", name="Apple",
        sector="Technology", shares=5, avg_cost_basis=150.0, target_weight=0.5,
    )
    db.add(pos2)
    await db.commit()
    await db.refresh(pos2)
    await lot_engine.open_lot(db, pos2.id, 5, 150.0, datetime.utcnow())
    await db.commit()

    foreign_lot = (await db.execute(
        select(TaxLot).where(TaxLot.position_id == pos2.id)
    )).scalar_one()

    with pytest.raises(ValueError, match="does not belong"):
        await execute_spec_id_sale(
            db, portfolio_id=port.id, position_id=pos.id,
            lot_ids=[foreign_lot.id], override_price=100.0,
        )


@pytest.mark.asyncio
async def test_spec_id_sell_rejects_already_closed_lot(db):
    port, pos = await _setup_portfolio_with_lots(db)
    lots = (await db.execute(select(TaxLot).where(TaxLot.position_id == pos.id))).scalars().all()
    target = lots[0]

    await execute_spec_id_sale(
        db, portfolio_id=port.id, position_id=pos.id,
        lot_ids=[target.id], override_price=100.0,
    )

    with pytest.raises(ValueError, match="already closed"):
        await execute_spec_id_sale(
            db, portfolio_id=port.id, position_id=pos.id,
            lot_ids=[target.id], override_price=100.0,
        )


@pytest.mark.asyncio
async def test_spec_id_sell_wash_sale_disallowance(db):
    """If the symbol was purchased within 30 days before sale, the loss is disallowed."""
    port, pos = await _setup_portfolio_with_lots(db)
    # Record a recent BUY to trigger wash-sale
    recent_buy = Transaction(
        portfolio_id=port.id, symbol="MSFT",
        transaction_type="BUY", shares=5, price=120.0, total_value=600.0,
        timestamp=datetime.utcnow() - timedelta(days=10),
    )
    db.add(recent_buy)
    await db.commit()

    lots = (await db.execute(select(TaxLot).where(TaxLot.position_id == pos.id))).scalars().all()
    hi_lot = next(l for l in lots if l.cost_basis == 130.0)

    r = await execute_spec_id_sale(
        db, portfolio_id=port.id, position_id=pos.id,
        lot_ids=[hi_lot.id], override_price=100.0,  # triggers loss
    )

    assert r["wash_sale_triggered"] is True
    assert r["wash_sale_disallowed"] == pytest.approx(300.0)
    # Recognizable loss = economic loss + disallowance (smaller in magnitude)
    assert r["recognizable_gain_loss"] == pytest.approx(0.0)
