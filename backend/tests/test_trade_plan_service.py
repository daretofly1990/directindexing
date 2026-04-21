"""Tests for the TradePlan lifecycle service."""
from datetime import datetime, timedelta

import pytest

from backend.models.models import Portfolio, TradePlan, AuditEvent
from backend.services import trade_plan_service


async def _mk_portfolio(db):
    port = Portfolio(name="T", initial_value=10_000, cash=10_000)
    db.add(port)
    await db.commit()
    await db.refresh(port)
    return port


@pytest.mark.asyncio
async def test_create_plan_persists_items_and_audits(db):
    port = await _mk_portfolio(db)
    draft = {
        "sells": [{"symbol": "AAPL", "shares": 10, "price": 180, "lot_ids": [1, 2]}],
        "buys":  [{"symbol": "MSFT", "shares": 5, "price": 340}],
    }
    plan = await trade_plan_service.create_trade_plan(db, port.id, draft, user_id=None)
    await db.refresh(plan, ["items"])
    assert plan.status == "DRAFT"
    assert len(plan.items) == 2
    actions = sorted(i.action for i in plan.items)
    assert actions == ["BUY", "SELL"]
    # Audit event written
    r = await db.execute(__import__("sqlalchemy").select(AuditEvent))
    events = r.scalars().all()
    assert any(e.event_type == "TRADE_PLAN_CREATED" for e in events)


@pytest.mark.asyncio
async def test_approve_then_executed(db):
    port = await _mk_portfolio(db)
    plan = await trade_plan_service.create_trade_plan(
        db, port.id, {"sells": [{"symbol": "AAPL", "shares": 1}], "buys": []}, user_id=None,
    )
    plan = await trade_plan_service.approve_plan(db, plan.id, user_id=None)
    assert plan.status == "APPROVED"
    plan = await trade_plan_service.mark_executed(db, plan.id, user_id=None)
    assert plan.status == "EXECUTED"


@pytest.mark.asyncio
async def test_cannot_approve_expired_plan(db):
    port = await _mk_portfolio(db)
    plan = await trade_plan_service.create_trade_plan(
        db, port.id, {"sells": [{"symbol": "AAPL", "shares": 1}], "buys": []}, user_id=None,
    )
    plan.expires_at = datetime.utcnow() - timedelta(hours=1)
    await db.commit()
    with pytest.raises(ValueError, match="expired"):
        await trade_plan_service.approve_plan(db, plan.id, user_id=None)
    # Status should have been flipped to EXPIRED as a side effect
    refreshed = await db.get(TradePlan, plan.id)
    assert refreshed.status == "EXPIRED"


@pytest.mark.asyncio
async def test_cancel_blocks_further_transitions(db):
    port = await _mk_portfolio(db)
    plan = await trade_plan_service.create_trade_plan(
        db, port.id, {"sells": [{"symbol": "AAPL", "shares": 1}], "buys": []}, user_id=None,
    )
    plan = await trade_plan_service.cancel_plan(db, plan.id, user_id=None)
    assert plan.status == "CANCELLED"
    with pytest.raises(ValueError):
        await trade_plan_service.approve_plan(db, plan.id, user_id=None)
