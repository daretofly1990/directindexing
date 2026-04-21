"""
Tests for the five security items:
  1. Idempotency keys prevent duplicate execution
  2. Kill switch halts trade execution
  3. Manual sell cap enforces the 30%-of-NAV limit
  4. AuditEvent rows are written for every sell/harvest/import
  5. PII encryption round-trips correctly when a key is configured
"""
import base64
import importlib
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend.models.models import (
    Portfolio, Position, Transaction, AuditEvent, SystemFlag, IdempotencyRecord,
)
from backend.services import kill_switch, idempotency, sell_service, ai_guardrails
from backend.services.lot_engine import lot_engine


# -------------------- helpers --------------------

async def _mk_portfolio_with_lot(db, symbol="AAPL", shares=100, basis=100, price=100):
    port = Portfolio(name="T", initial_value=100_000, cash=0)
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


# -------------------- 1. idempotency --------------------

@pytest.mark.asyncio
async def test_idempotency_cache_hit_returns_stored_response(db):
    port = Portfolio(name="T", initial_value=1000, cash=0)
    # create a user to own the idempotency record
    from backend.models.models import User
    user = User(email="a@b.com", hashed_password="x", role="individual")
    db.add_all([port, user])
    await db.commit()
    await db.refresh(user)

    key = "uuid-abc"
    endpoint = "sell"
    resp = {"foo": "bar"}
    await idempotency.cache_response(db, key, user.id, endpoint, resp)

    got = await idempotency.get_cached_response(db, key, user.id, endpoint)
    assert got == resp


@pytest.mark.asyncio
async def test_idempotency_returns_none_on_unknown_key(db):
    from backend.models.models import User
    user = User(email="a@b.com", hashed_password="x", role="individual")
    db.add(user); await db.commit(); await db.refresh(user)
    assert await idempotency.get_cached_response(db, "nope", user.id, "sell") is None


# -------------------- 2. kill switch --------------------

@pytest.mark.asyncio
async def test_kill_switch_default_not_halted(db):
    kill_switch._cache["ts"] = 0.0
    halted, reason = await kill_switch.is_halted(db)
    assert halted is False
    assert reason is None


@pytest.mark.asyncio
async def test_kill_switch_set_and_get(db):
    kill_switch._cache["ts"] = 0.0
    await kill_switch.set_halted(db, True, "SIPC liquidation drill", user_id=None)
    halted, reason = await kill_switch.is_halted(db)
    assert halted is True
    assert reason == "SIPC liquidation drill"


@pytest.mark.asyncio
async def test_kill_switch_blocks_spec_id_sale(db, monkeypatch):
    kill_switch._cache["ts"] = 0.0
    port, pos = await _mk_portfolio_with_lot(db)
    await kill_switch.set_halted(db, True, "halt", user_id=None)

    lots = (await db.execute(select(Position).where(Position.id == pos.id))).scalar_one()
    from backend.models.models import TaxLot
    lot = (await db.execute(select(TaxLot).where(TaxLot.position_id == pos.id))).scalar_one()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await sell_service.execute_spec_id_sale(
            db, port.id, pos.id, [lot.id], override_price=110.0,
        )
    assert exc.value.status_code == 503


# -------------------- 3. sell cap --------------------

@pytest.mark.asyncio
async def test_manual_sell_cap_blocks_large_sale(db, monkeypatch):
    """Selling 100% of a single-position portfolio breaks the 30% cap."""
    kill_switch._cache["ts"] = 0.0
    port, pos = await _mk_portfolio_with_lot(db, shares=100, basis=100)

    async def fake_quotes(symbols):
        return {s: {"current_price": 150.0} for s in symbols}
    monkeypatch.setattr(ai_guardrails.finnhub_client, "get_multiple_quotes", fake_quotes)

    from backend.models.models import TaxLot
    lot = (await db.execute(select(TaxLot).where(TaxLot.position_id == pos.id))).scalar_one()

    with pytest.raises(ValueError, match="SELL_CAP_EXCEEDED"):
        await sell_service.execute_spec_id_sale(
            db, port.id, pos.id, [lot.id], override_price=150.0,
        )


@pytest.mark.asyncio
async def test_manual_sell_cap_allows_small_sale(db, monkeypatch):
    kill_switch._cache["ts"] = 0.0
    # Two positions so one sale is a small fraction of NAV
    port = Portfolio(name="T", initial_value=10_000, cash=0)
    db.add(port); await db.commit(); await db.refresh(port)
    p1 = Position(
        portfolio_id=port.id, symbol="AAPL", name="AAPL",
        sector="Tech", shares=10, avg_cost_basis=100, target_weight=0.1,
    )
    p2 = Position(
        portfolio_id=port.id, symbol="MSFT", name="MSFT",
        sector="Tech", shares=1000, avg_cost_basis=100, target_weight=0.9,
    )
    db.add_all([p1, p2]); await db.commit()
    await db.refresh(p1); await db.refresh(p2)
    await lot_engine.open_lot(db, p1.id, 10, 100, datetime(2023, 1, 1))
    await db.commit()

    async def fake_quotes(symbols):
        return {s: {"current_price": 100.0} for s in symbols}
    monkeypatch.setattr(ai_guardrails.finnhub_client, "get_multiple_quotes", fake_quotes)

    async def fake_quote(symbol):
        return {"current_price": 100.0}
    monkeypatch.setattr(sell_service.finnhub_client, "get_quote", fake_quote)

    from backend.models.models import TaxLot
    lot = (await db.execute(select(TaxLot).where(TaxLot.position_id == p1.id))).scalar_one()

    # 10 shares * $100 = $1000, NAV ~= $101k → 1% — well within 30% cap
    result = await sell_service.execute_spec_id_sale(
        db, port.id, p1.id, [lot.id],
    )
    assert result["shares_sold"] == pytest.approx(10.0)


# -------------------- 4. audit events --------------------

@pytest.mark.asyncio
async def test_manual_sell_writes_audit_event(db, monkeypatch):
    kill_switch._cache["ts"] = 0.0
    # Build portfolio where sale is under cap
    port = Portfolio(name="T", initial_value=10_000, cash=0)
    db.add(port); await db.commit(); await db.refresh(port)
    p = Position(
        portfolio_id=port.id, symbol="AAPL", name="AAPL",
        sector="Tech", shares=1000, avg_cost_basis=100, target_weight=1.0,
    )
    db.add(p); await db.commit(); await db.refresh(p)
    await lot_engine.open_lot(db, p.id, 100, 100, datetime(2023, 1, 1))  # selling 100 of 1000
    await db.commit()

    async def fake_quotes(symbols): return {s: {"current_price": 100.0} for s in symbols}
    async def fake_quote(symbol): return {"current_price": 100.0}
    monkeypatch.setattr(ai_guardrails.finnhub_client, "get_multiple_quotes", fake_quotes)
    monkeypatch.setattr(sell_service.finnhub_client, "get_quote", fake_quote)

    from backend.models.models import TaxLot
    lot = (await db.execute(select(TaxLot).where(TaxLot.position_id == p.id))).scalar_one()

    await sell_service.execute_spec_id_sale(
        db, port.id, p.id, [lot.id], user_id=42, ip_address="1.2.3.4",
    )

    events = (await db.execute(select(AuditEvent))).scalars().all()
    sell_events = [e for e in events if e.event_type == "MANUAL_SELL"]
    assert len(sell_events) == 1
    assert sell_events[0].user_id == 42
    assert sell_events[0].portfolio_id == port.id
    assert sell_events[0].ip_address == "1.2.3.4"


# -------------------- 5. encryption --------------------

def test_encryption_roundtrip_with_key():
    """Reload the encryption module after setting a key — verify cipher/decipher."""
    key = base64.urlsafe_b64encode(b"\x00" * 32).decode()
    with patch.dict(os.environ, {"FIELD_ENCRYPTION_KEYS": key}):
        from backend.services import encryption
        importlib.reload(encryption)
        ct = encryption.encrypt("hello world")
        assert ct is not None
        assert ct.startswith(encryption.MARKER)
        assert encryption.decrypt(ct) == "hello world"


def test_encryption_noop_without_key():
    with patch.dict(os.environ, {"FIELD_ENCRYPTION_KEYS": ""}, clear=False):
        # Remove the key if it happens to be set
        os.environ.pop("FIELD_ENCRYPTION_KEYS", None)
        from backend.services import encryption
        importlib.reload(encryption)
        assert encryption.encrypt("hello") == "hello"   # no-op
        assert encryption.decrypt("hello") == "hello"   # no marker, passthrough


def test_encryption_passes_through_legacy_plaintext():
    """Decrypt should return legacy (unmarked) plaintext rows as-is."""
    key = base64.urlsafe_b64encode(b"\x01" * 32).decode()
    with patch.dict(os.environ, {"FIELD_ENCRYPTION_KEYS": key}):
        from backend.services import encryption
        importlib.reload(encryption)
        assert encryption.decrypt("plain legacy text") == "plain legacy text"
