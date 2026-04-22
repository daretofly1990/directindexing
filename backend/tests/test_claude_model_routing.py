"""
Tests for per-tier Claude model routing.

The contract:
  - No subscription → "default"; model = CLAUDE_MODEL_DEFAULT
  - Subscription active + tier=premium → "premium"; model = CLAUDE_MODEL_PREMIUM
  - Subscription trialing + tier=premium → "premium" (let trial-users feel
    what they're paying for)
  - Cancelled / past_due / expired → treated as "default"
"""
import pytest

from backend.models.models import Subscription, User
from backend.services import billing_service
from backend.services.billing_service import (
    get_active_tier, get_claude_model_for_user,
)


@pytest.fixture(autouse=True)
def _set_models(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "CLAUDE_MODEL_DEFAULT", "claude-haiku-TEST")
    monkeypatch.setattr(billing_service.settings, "CLAUDE_MODEL_PREMIUM", "claude-opus-TEST")


async def _mk_user(db, email="u@t.com"):
    u = User(email=email, hashed_password="x", role="individual")
    db.add(u); await db.commit(); await db.refresh(u)
    return u


@pytest.mark.asyncio
async def test_no_subscription_defaults_to_default_tier(db):
    u = await _mk_user(db)
    assert await get_active_tier(db, u.id) == "default"
    assert await get_claude_model_for_user(db, u.id) == "claude-haiku-TEST"


@pytest.mark.asyncio
async def test_active_premium_gets_premium_model(db):
    u = await _mk_user(db)
    db.add(Subscription(user_id=u.id, tier="premium", status="active"))
    await db.commit()
    assert await get_active_tier(db, u.id) == "premium"
    assert await get_claude_model_for_user(db, u.id) == "claude-opus-TEST"


@pytest.mark.asyncio
async def test_trialing_premium_also_gets_premium_model(db):
    u = await _mk_user(db)
    db.add(Subscription(user_id=u.id, tier="premium", status="trialing"))
    await db.commit()
    assert await get_claude_model_for_user(db, u.id) == "claude-opus-TEST"


@pytest.mark.asyncio
async def test_starter_standard_get_default_model(db):
    for tier in ("starter", "standard"):
        u = await _mk_user(db, email=f"{tier}@t.com")
        db.add(Subscription(user_id=u.id, tier=tier, status="active"))
        await db.commit()
        assert await get_claude_model_for_user(db, u.id) == "claude-haiku-TEST", tier


@pytest.mark.asyncio
async def test_cancelled_subscription_falls_back_to_default(db):
    u = await _mk_user(db)
    db.add(Subscription(user_id=u.id, tier="premium", status="canceled"))
    await db.commit()
    assert await get_active_tier(db, u.id) == "default"
    assert await get_claude_model_for_user(db, u.id) == "claude-haiku-TEST"


@pytest.mark.asyncio
async def test_past_due_falls_back_to_default(db):
    u = await _mk_user(db)
    db.add(Subscription(user_id=u.id, tier="premium", status="past_due"))
    await db.commit()
    assert await get_claude_model_for_user(db, u.id) == "claude-haiku-TEST"


@pytest.mark.asyncio
async def test_identical_model_envs_route_everyone_to_same(db, monkeypatch):
    """During dev both env vars set to the same model — nobody sees a difference."""
    monkeypatch.setattr(billing_service.settings, "CLAUDE_MODEL_DEFAULT", "claude-opus-4-5")
    monkeypatch.setattr(billing_service.settings, "CLAUDE_MODEL_PREMIUM", "claude-opus-4-5")
    u_free = await _mk_user(db, email="free@t.com")
    u_prem = await _mk_user(db, email="prem@t.com")
    db.add(Subscription(user_id=u_prem.id, tier="premium", status="active"))
    await db.commit()
    assert await get_claude_model_for_user(db, u_free.id) == "claude-opus-4-5"
    assert await get_claude_model_for_user(db, u_prem.id) == "claude-opus-4-5"
