"""
Tests for the annual Reg S-P re-acceptance staleness check and the market-cap cache.
"""
from datetime import datetime, timedelta

import pytest

from backend.models.models import User, Acknowledgement
from backend.api.routes.acknowledgements import (
    user_has_accepted, CURRENT_VERSIONS, ANNUAL_REACCEPT_DOCS, REACCEPT_MAX_AGE_DAYS,
)
from backend.services import constituents as c


# --------------- Annual ack staleness ---------------

@pytest.mark.asyncio
async def test_fresh_ack_returns_true(db):
    u = User(email="f@t.com", hashed_password="x", role="individual")
    db.add(u); await db.commit(); await db.refresh(u)
    db.add(Acknowledgement(
        user_id=u.id, document_type="privacy",
        version=CURRENT_VERSIONS["privacy"],
        accepted_at=datetime.utcnow() - timedelta(days=30),
    ))
    await db.commit()
    assert await user_has_accepted(db, u.id, "privacy") is True


@pytest.mark.asyncio
async def test_stale_privacy_ack_returns_false(db):
    u = User(email="s@t.com", hashed_password="x", role="individual")
    db.add(u); await db.commit(); await db.refresh(u)
    db.add(Acknowledgement(
        user_id=u.id, document_type="privacy",
        version=CURRENT_VERSIONS["privacy"],
        accepted_at=datetime.utcnow() - timedelta(days=REACCEPT_MAX_AGE_DAYS + 10),
    ))
    await db.commit()
    assert await user_has_accepted(db, u.id, "privacy") is False


@pytest.mark.asyncio
async def test_stale_tos_ack_still_returns_true(db):
    """ToS is NOT in the annual reaccept set — stale is still accepted."""
    u = User(email="t@t.com", hashed_password="x", role="individual")
    db.add(u); await db.commit(); await db.refresh(u)
    db.add(Acknowledgement(
        user_id=u.id, document_type="tos",
        version=CURRENT_VERSIONS["tos"],
        accepted_at=datetime.utcnow() - timedelta(days=REACCEPT_MAX_AGE_DAYS + 100),
    ))
    await db.commit()
    assert "tos" not in ANNUAL_REACCEPT_DOCS
    assert await user_has_accepted(db, u.id, "tos") is True


@pytest.mark.asyncio
async def test_no_ack_returns_false(db):
    u = User(email="n@t.com", hashed_password="x", role="individual")
    db.add(u); await db.commit(); await db.refresh(u)
    assert await user_has_accepted(db, u.id, "privacy") is False


@pytest.mark.asyncio
async def test_annual_reaccept_docs_are_configured_correctly():
    """Sanity check — privacy must be in the reaccept set (Reg S-P requirement)."""
    assert "privacy" in ANNUAL_REACCEPT_DOCS
    assert "adv_part_2a" in ANNUAL_REACCEPT_DOCS


# --------------- Market-cap cache ---------------

def test_mcap_cache_hit_returns_value():
    c.clear_mcap_cache()
    c._cache_set("AAPL", 3_000_000_000_000.0)
    hit, val = c._cache_get("AAPL")
    assert hit is True
    assert val == 3_000_000_000_000.0


def test_mcap_cache_miss_on_unknown():
    c.clear_mcap_cache()
    hit, val = c._cache_get("NEVER_SEEN")
    assert hit is False
    assert val is None


def test_mcap_cache_stores_none_for_failed_lookups():
    """Caching None ('not found') is important — otherwise we'd retry every refresh."""
    c.clear_mcap_cache()
    c._cache_set("DELISTED", None)
    hit, val = c._cache_get("DELISTED")
    assert hit is True   # we've cached the fact that there's no value
    assert val is None


def test_mcap_cache_expires_after_ttl(monkeypatch):
    """Entries past TTL should miss and be purged."""
    c.clear_mcap_cache()
    import backend.services.constituents as _c

    # Simulate caching an entry 25 hours ago
    _c._mcap_cache["STALE"] = (100.0, _c._time.monotonic() - (25 * 3600))
    hit, val = c._cache_get("STALE")
    assert hit is False
    assert "STALE" not in _c._mcap_cache
