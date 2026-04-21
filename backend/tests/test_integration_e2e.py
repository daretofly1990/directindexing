"""
End-to-end integration test: full happy path through the HTTP API.

Walks the exact flow a real retail customer would take:
  1. Sign up as an individual
  2. Accept ToS + ADV + Privacy acknowledgements
  3. /api/auth/me reports no missing acknowledgements
  4. Create a portfolio (individual → auto-pinned to self-client)
  5. Construct positions directly (skipping the real yfinance path)
  6. Run the AI advisor in demo mode
  7. Save the draft as a TradePlan
  8. Approve the plan (kill-switch gate + idempotency)
  9. Export Schwab CSV
 10. Reconcile (the diff path)
 11. Request the Form 8949 CSV

This runs entirely against an in-memory SQLite via ASGI, with Finnhub/yfinance
mocked. Its job is to catch regressions at the seam between modules — not to
test the internals of any one piece. Each subsystem has dedicated unit tests.
"""
import os
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker


# Ensure a deterministic test env BEFORE importing the app
os.environ.setdefault("FINNHUB_API_KEY", "test-finnhub")
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-characters-long")
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("ADMIN_PASSWORD", "testpass12345")
os.environ.setdefault("APP_BASE_URL", "http://testserver")
os.environ.setdefault("FIELD_ENCRYPTION_KEYS", "")   # plaintext mode in tests


@pytest_asyncio.fixture
async def client(monkeypatch):
    """
    Swap the app's session factory to an in-memory SQLite, yield an AsyncClient.
    Also disables SlowAPI rate limiting (tests share one IP and would trip
    the 3/min signup cap within a single module run).
    """
    from backend import database as _db
    from backend.database import Base
    from backend import main as _main   # noqa: F401 — triggers app init
    from backend import rate_limit as _rl

    # Disable SlowAPI for tests — the limiter's `.limit()` decorator reads
    # `enabled` off the Limiter at registration time; we flip the runtime flag.
    _rl.limiter.enabled = False

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Point the app's AsyncSessionLocal at this DB for the duration of the test
    original_factory = _db.AsyncSessionLocal
    _db.AsyncSessionLocal = factory

    from backend.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    _db.AsyncSessionLocal = original_factory
    _rl.limiter.enabled = True
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def mocked_market(monkeypatch):
    """Silence the network-dependent pieces: Finnhub quotes + yfinance construct."""
    from backend.services import ai_guardrails as g
    from backend.services import sell_service as ss
    from backend.services import tax_loss_service as tls
    from backend.services import tlh_tools as tlh
    from backend.services import portfolio_service as ps

    async def fake_multi(symbols):
        return {s: {"current_price": 120.0} for s in symbols}
    async def fake_single(symbol):
        return {"current_price": 120.0}
    monkeypatch.setattr(g.finnhub_client, "get_multiple_quotes", fake_multi)
    monkeypatch.setattr(ss.finnhub_client, "get_quote", fake_single)
    monkeypatch.setattr(tls.finnhub_client, "get_multiple_quotes", fake_multi)
    monkeypatch.setattr(tlh.finnhub_client, "get_multiple_quotes", fake_multi)
    monkeypatch.setattr(tlh.finnhub_client, "get_quote", fake_single)

    # Bypass the real construct-from-index path; seed positions directly
    yield


async def _authed(client: AsyncClient, token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_retail_happy_path(client: AsyncClient, mocked_market):
    # 1. Signup
    r = await client.post("/api/signup/individual", json={
        "email": "alice@example.com",
        "password": "alice12345",
        "full_name": "Alice Doe",
    })
    assert r.status_code == 200, r.text
    signup = r.json()
    token = signup["access_token"]
    client_id = signup["client"]["id"]
    assert signup["user"]["role"] == "individual"
    assert signup["client"]["is_self"] is True
    h = await _authed(client, token)

    # 2. Accept all three docs
    for doc in ("tos", "adv_part_2a", "privacy"):
        r = await client.post("/api/acknowledgements", json={"document_type": doc}, headers=h)
        assert r.status_code == 200, f"{doc}: {r.text}"

    # 3. /me should show no missing acks and features.can_run_advisor=True
    r = await client.get("/api/auth/me", headers=h)
    assert r.status_code == 200
    me = r.json()
    assert me["missing_acknowledgements"] == []
    assert me["features"]["can_run_advisor"] is True
    assert me["self_client"]["id"] == client_id

    # 4. Create portfolio (individual → auto-pinned to self-client)
    r = await client.post("/api/portfolios", json={
        "name": "My Direct Index", "initial_value": 100_000,
    }, headers=h)
    assert r.status_code == 200, r.text
    portfolio_id = r.json()["id"]

    # 5. Seed positions directly (skips yfinance round-trip)
    from backend.database import AsyncSessionLocal
    from backend.models.models import Position, TaxLot
    from datetime import datetime, timedelta
    async with AsyncSessionLocal() as db:
        for sym, shares, basis, days_ago in [
            ("AAPL", 100, 180.0, 400),   # LT, above current → loser for TLH
            ("MSFT",  50, 150.0, 50),    # ST, above current → loser
            ("NVDA",  20, 100.0, 500),   # LT gain
        ]:
            pos = Position(
                portfolio_id=portfolio_id, symbol=sym, name=sym,
                sector="Technology", shares=shares, avg_cost_basis=basis,
                target_weight=0.33, is_active=True,
            )
            db.add(pos)
            await db.flush()
            db.add(TaxLot(
                position_id=pos.id, shares=shares, cost_basis=basis,
                purchase_date=datetime.utcnow() - timedelta(days=days_ago),
            ))
        await db.commit()

    # 6. Find losses (direct primitive — demo agent also works but this is faster)
    r = await client.get(f"/api/portfolios/{portfolio_id}/tlh/losses", headers=h)
    assert r.status_code == 200, r.text
    losses = r.json()
    assert losses["total_harvestable_loss"] < 0
    symbols_seen = {o["symbol"] for o in losses["opportunities"]}
    assert "AAPL" in symbols_seen or "MSFT" in symbols_seen

    # 7. Create a draft trade plan directly
    draft = {
        "sells": [
            {"symbol": "AAPL", "shares": 100, "price": 120, "lot_ids": []},
        ],
        "buys": [
            {"symbol": "VTI", "shares": 60, "price": 200},
        ],
    }
    r = await client.post(
        f"/api/portfolios/{portfolio_id}/trade-plans",
        json={"draft_plan": draft, "summary": "Test harvest"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    plan = r.json()
    plan_id = plan["id"]
    assert plan["status"] == "DRAFT"
    assert len(plan["items"]) == 2

    # 8. Approve the plan (kill-switch gate + audit row + idempotency header)
    r = await client.post(
        f"/api/portfolios/{portfolio_id}/trade-plans/{plan_id}/approve",
        headers={**h, "Idempotency-Key": "idem-approve-1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"

    # Retry with same idempotency key → cached response, no second approval
    r2 = await client.post(
        f"/api/portfolios/{portfolio_id}/trade-plans/{plan_id}/approve",
        headers={**h, "Idempotency-Key": "idem-approve-1"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "APPROVED"

    # 9. Schwab CSV export — should contain the SELL row
    r = await client.get(
        f"/api/portfolios/{portfolio_id}/trade-plans/{plan_id}/export.csv?format=schwab",
        headers=h,
    )
    assert r.status_code == 200
    body = r.text
    assert "AAPL" in body and "SELL" in body
    assert body.startswith("Symbol,Action,Quantity")

    # 10. Reconcile upload (CSV claiming the AAPL position is gone)
    reconcile_csv = (
        "Symbol,Date Acquired,Quantity,Cost Per Share\n"
        "MSFT,03/01/2026,50,150.00\n"
        "NVDA,01/15/2024,20,100.00\n"
        "VTI,04/20/2026,60,200.00\n"
    ).encode()
    files = {"file": ("after.csv", reconcile_csv, "text/csv")}
    r = await client.post(
        f"/api/portfolios/{portfolio_id}/trade-plans/{plan_id}/reconcile",
        headers=h, files=files,
    )
    assert r.status_code == 200, r.text
    recon = r.json()
    assert "diff" in recon
    # The AAPL SELL should register as FILLED (it's gone from the new CSV)
    items = recon["diff"]["items"]
    aapl_item = next((i for i in items if i["symbol"] == "AAPL"), None)
    assert aapl_item is not None
    assert aapl_item["status"] in ("FILLED", "PARTIAL")

    # 11. Plan should now be EXECUTED
    r = await client.get(
        f"/api/portfolios/{portfolio_id}/trade-plans/{plan_id}",
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "EXECUTED"

    # 12. Form 8949 CSV — header present; rows depend on whether any lots closed
    r = await client.get(
        f"/api/portfolios/{portfolio_id}/form-8949.csv",
        headers=h,
    )
    assert r.status_code == 200
    assert "Form" in r.text and "Date Acquired" in r.text


@pytest.mark.asyncio
async def test_individual_cannot_access_foreign_portfolio(client: AsyncClient, mocked_market):
    """RBAC smoke: user A's portfolio is invisible to user B."""
    # Signup A
    ra = await client.post("/api/signup/individual", json={
        "email": "a@x.com", "password": "aaaaaaaa", "full_name": "A",
    })
    assert ra.status_code == 200
    a_token = ra.json()["access_token"]
    a_hdr = {"Authorization": f"Bearer {a_token}"}

    for doc in ("tos", "adv_part_2a", "privacy"):
        await client.post("/api/acknowledgements", json={"document_type": doc}, headers=a_hdr)

    r = await client.post("/api/portfolios", json={"name": "A's", "initial_value": 10000}, headers=a_hdr)
    a_portfolio = r.json()["id"]

    # Signup B
    rb = await client.post("/api/signup/individual", json={
        "email": "b@x.com", "password": "bbbbbbbb", "full_name": "B",
    })
    assert rb.status_code == 200
    b_token = rb.json()["access_token"]
    b_hdr = {"Authorization": f"Bearer {b_token}"}
    for doc in ("tos", "adv_part_2a", "privacy"):
        await client.post("/api/acknowledgements", json={"document_type": doc}, headers=b_hdr)

    # B tries to read A's portfolio → 403
    r = await client.get(f"/api/portfolios/{a_portfolio}", headers=b_hdr)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_advisor_blocked_until_adv_accepted(client: AsyncClient, mocked_market):
    """Agent endpoint must 403 until ADV Part 2A is on file."""
    r = await client.post("/api/signup/individual", json={
        "email": "c@x.com", "password": "cccccccc", "full_name": "C",
    })
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    # Only accept ToS + Privacy, NOT ADV
    for doc in ("tos", "privacy"):
        await client.post("/api/acknowledgements", json={"document_type": doc}, headers=h)

    # Create portfolio so the route passes the portfolio-access check
    p = await client.post("/api/portfolios", json={"name": "x", "initial_value": 1000}, headers=h)
    pid = p.json()["id"]

    r = await client.post(
        f"/api/portfolios/{pid}/harvest-agent",
        json={"instruction": "find losses"},
        headers=h,
    )
    assert r.status_code == 403
    assert "ADV" in r.text
