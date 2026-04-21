"""
Tests for the Invite-your-CPA magic-link flow.

Covers:
  - Service-layer create/resolve/revoke/view-payload
  - Token reuse after revocation is rejected
  - Token with wrong jti-hash is rejected (prevents token rotation replay)
  - View payload shape: summary totals, ST/LT split, wash-sale flag
"""
from datetime import datetime, timedelta

import jwt
import pytest

from backend.models.models import CPAInvite, Portfolio, Position, TaxLot, User
from backend.services.cpa_invite_service import (
    _hash_jti,
    build_cpa_view_payload,
    create_cpa_invite,
    record_view,
    resolve_invite,
    revoke_invite,
)
from backend.services.lot_engine import lot_engine
from backend.services.user_service import create_individual_user


async def _setup_portfolio_with_closed_lots(db):
    user, _client = await create_individual_user(db, "mike@example.com", "pw12345")
    port = Portfolio(
        name="Test", initial_value=10_000, cash=0, client_id=_client.id,
    )
    db.add(port)
    await db.commit()
    await db.refresh(port)

    # Open a lot, then close it at a gain
    pos = Position(
        portfolio_id=port.id, symbol="AAPL", name="AAPL",
        sector="Tech", shares=100, avg_cost_basis=150.0, target_weight=1.0,
    )
    db.add(pos)
    await db.commit()
    await db.refresh(pos)

    # Long-term lot: purchased 2 years ago, sold today for a gain
    await lot_engine.open_lot(
        db, pos.id, 100, 150.0, datetime.utcnow() - timedelta(days=400),
    )
    await db.commit()
    lots = await lot_engine.get_open_lot_detail(db, port.id)
    lot_ids = [l["lot_id"] for l in lots]
    await lot_engine.close_lots_by_ids(
        db, lot_ids=lot_ids, sale_price=200.0, sale_date=datetime.utcnow(),
    )
    await db.commit()
    return user, port


@pytest.mark.asyncio
async def test_create_and_resolve_happy_path(db):
    user, port = await _setup_portfolio_with_closed_lots(db)
    invite, token = await create_cpa_invite(
        db, user=user, portfolio=port,
        cpa_email="cpa@example.com", firm_name="Acme CPA LLP",
        send=False,
    )
    resolved, status = await resolve_invite(db, token)
    assert status == "ok"
    assert resolved.id == invite.id
    assert resolved.firm_name == "Acme CPA LLP"


@pytest.mark.asyncio
async def test_invalid_token_raises(db):
    user, port = await _setup_portfolio_with_closed_lots(db)
    invite, token = await create_cpa_invite(
        db, user=user, portfolio=port,
        cpa_email="cpa@example.com", send=False,
    )
    # Break the signature by flipping one char in the body
    bad = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    with pytest.raises(jwt.InvalidTokenError):
        await resolve_invite(db, bad)


@pytest.mark.asyncio
async def test_expired_invite_returns_status(db, monkeypatch):
    user, port = await _setup_portfolio_with_closed_lots(db)
    invite, token = await create_cpa_invite(
        db, user=user, portfolio=port,
        cpa_email="cpa@example.com",
        ttl=timedelta(seconds=30),  # short TTL for test
        send=False,
    )
    # Backdate expiration
    invite.expires_at = datetime.utcnow() - timedelta(days=1)
    await db.commit()
    resolved, status = await resolve_invite(db, token)
    assert status == "expired"


@pytest.mark.asyncio
async def test_revoked_invite_returns_status(db):
    user, port = await _setup_portfolio_with_closed_lots(db)
    invite, token = await create_cpa_invite(
        db, user=user, portfolio=port,
        cpa_email="cpa@example.com", send=False,
    )
    await revoke_invite(db, invite)
    await db.commit()
    resolved, status = await resolve_invite(db, token)
    assert status == "revoked"


@pytest.mark.asyncio
async def test_rotated_token_jti_mismatch(db):
    """If token_hash is replaced after issue, old token can't be replayed."""
    user, port = await _setup_portfolio_with_closed_lots(db)
    invite, token = await create_cpa_invite(
        db, user=user, portfolio=port,
        cpa_email="cpa@example.com", send=False,
    )
    # Simulate a rotated jti: admin reissues a new token for this invite row,
    # which would overwrite token_hash. The original token should now fail.
    invite.token_hash = _hash_jti("different-jti")
    await db.commit()
    with pytest.raises(LookupError):
        await resolve_invite(db, token)


@pytest.mark.asyncio
async def test_view_payload_shape(db):
    user, port = await _setup_portfolio_with_closed_lots(db)
    invite, _token = await create_cpa_invite(
        db, user=user, portfolio=port,
        cpa_email="cpa@example.com", cpa_name="Jane", firm_name="Acme",
        send=False,
    )
    payload = await build_cpa_view_payload(db, invite)
    assert payload["portfolio_id"] == port.id
    assert payload["cpa"]["email"] == "cpa@example.com"
    # Long-term gain of (200-150) * 100 = $5000
    assert payload["summary"]["total_realized_gain_long_term"] == pytest.approx(5000.0)
    assert payload["summary"]["total_realized_gain_short_term"] == pytest.approx(0.0)
    assert payload["summary"]["closed_lot_count"] == 1
    assert payload["closed_lots"][0]["term"] == "long"
    # Any wash-sale disallowance should be 0 for this clean sale
    assert payload["closed_lots"][0]["wash_sale_code"] == ""


@pytest.mark.asyncio
async def test_record_view_bumps_counters(db):
    user, port = await _setup_portfolio_with_closed_lots(db)
    invite, _token = await create_cpa_invite(
        db, user=user, portfolio=port,
        cpa_email="cpa@example.com", send=False,
    )
    assert invite.view_count == 0
    assert invite.first_viewed_at is None
    await record_view(db, invite)
    await record_view(db, invite)
    await db.commit()
    await db.refresh(invite)
    assert invite.view_count == 2
    assert invite.first_viewed_at is not None
    assert invite.last_viewed_at >= invite.first_viewed_at
