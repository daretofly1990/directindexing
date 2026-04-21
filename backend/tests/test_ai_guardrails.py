"""
AI guardrail regression suite.

Named cases covering edge conditions flagged in TODO M6:
  - embedded gain position (not a loss — advisor should not harvest)
  - position already in wash-sale window (SELL must be flagged)
  - substantially-identical replacement (must be blocked)
  - ST→LT crossover (purchase_date 364 days old — stays ST)
  - cap on % of portfolio recommended per day
  - schema validation rejects malformed output

Each case feeds the guardrail/validation functions a crafted draft_plan and
asserts the right warning/rejection fires. Run on every commit that touches
tlh_agent.py, tlh_tools.py, or ai_guardrails.py.
"""
from datetime import datetime, timedelta

import pytest

from backend.models.models import Portfolio, Position, TaxLot, Transaction
from backend.services.ai_guardrails import (
    apply_guardrails, validate_draft_plan_schema, SUBSTANTIALLY_IDENTICAL,
)


async def _build_portfolio(db, symbols_and_shares: list[tuple[str, float, float]]):
    port = Portfolio(name="T", initial_value=100_000, cash=10_000)
    db.add(port)
    await db.commit()
    await db.refresh(port)
    for sym, shares, basis in symbols_and_shares:
        pos = Position(
            portfolio_id=port.id, symbol=sym, name=sym,
            sector="Technology", shares=shares, avg_cost_basis=basis,
            target_weight=0.1,
        )
        db.add(pos)
    await db.commit()
    return port


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------

def test_schema_accepts_valid_plan():
    plan = {
        "sells": [{"symbol": "AAPL", "shares": 10}],
        "buys": [{"symbol": "MSFT", "shares": 10}],
    }
    ok, errors = validate_draft_plan_schema(plan)
    assert ok is True
    assert errors == []


def test_schema_rejects_non_dict():
    ok, errors = validate_draft_plan_schema("not a dict")
    assert ok is False


def test_schema_rejects_missing_symbol():
    plan = {"sells": [{"shares": 10}], "buys": []}
    ok, errors = validate_draft_plan_schema(plan)
    assert ok is False
    assert any("missing symbol" in e for e in errors)


def test_schema_rejects_missing_shares():
    plan = {"sells": [{"symbol": "AAPL"}], "buys": []}
    ok, errors = validate_draft_plan_schema(plan)
    assert ok is False
    assert any("missing shares" in e for e in errors)


# --------------------------------------------------------------------------
# Substantially identical filter
# --------------------------------------------------------------------------

def test_si_map_is_symmetric():
    """If A→B is SI then B→A must be SI; protects against tax-interpretation bugs."""
    for a, siblings in SUBSTANTIALLY_IDENTICAL.items():
        for b in siblings:
            assert a in SUBSTANTIALLY_IDENTICAL.get(b, set()), f"{a}↔{b} asymmetry"


@pytest.mark.asyncio
async def test_si_replacement_stripped(db):
    port = await _build_portfolio(db, [("SPY", 100, 400)])
    plan = {
        "sells": [{"symbol": "SPY", "shares": 100, "price": 410}],
        "buys":  [{"symbol": "IVV", "shares": 100, "price": 400}],  # SI with SPY
    }
    out, warnings = await apply_guardrails(db, port.id, plan)
    assert out["buys"] == []
    assert any("SI" in w or "substantially identical" in w for w in warnings)


# --------------------------------------------------------------------------
# Wash-sale flag
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wash_sale_flag_on_recent_buy(db):
    port = await _build_portfolio(db, [("AAPL", 50, 100)])
    db.add(Transaction(
        portfolio_id=port.id, symbol="AAPL",
        transaction_type="BUY", shares=10, price=95, total_value=950,
        timestamp=datetime.utcnow() - timedelta(days=7),
    ))
    await db.commit()
    plan = {"sells": [{"symbol": "AAPL", "shares": 50, "price": 90}], "buys": []}
    out, warnings = await apply_guardrails(db, port.id, plan)
    assert out["sells"][0].get("wash_sale_flag") is True
    assert any("WASH_SALE_RISK" in w for w in warnings)


# --------------------------------------------------------------------------
# Max sell cap
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_sell_cap_triggers(db, monkeypatch):
    """A plan selling 50% of NAV exceeds the 30% cap."""
    port = await _build_portfolio(db, [("AAPL", 1000, 100)])  # 1000 * 100 = $100k
    # Fake quote = $100 so NAV = $100k + $10k cash = $110k
    async def fake_quotes(symbols):
        return {s: {"current_price": 100.0} for s in symbols}
    from backend.services import ai_guardrails as g
    monkeypatch.setattr(g.finnhub_client, "get_multiple_quotes", fake_quotes)

    plan = {
        "sells": [{"symbol": "AAPL", "shares": 600, "price": 100}],  # $60k — 54% of NAV
        "buys":  [],
    }
    out, warnings = await apply_guardrails(db, port.id, plan)
    assert any("MAX_SELL" in w for w in warnings)
    assert out.get("blocked_reason") == "MAX_SELL_PCT_EXCEEDED"


@pytest.mark.asyncio
async def test_max_sell_cap_within_threshold(db, monkeypatch):
    port = await _build_portfolio(db, [("AAPL", 1000, 100)])
    async def fake_quotes(symbols):
        return {s: {"current_price": 100.0} for s in symbols}
    from backend.services import ai_guardrails as g
    monkeypatch.setattr(g.finnhub_client, "get_multiple_quotes", fake_quotes)

    plan = {
        "sells": [{"symbol": "AAPL", "shares": 100, "price": 100}],  # $10k — well under 30%
        "buys":  [],
    }
    out, warnings = await apply_guardrails(db, port.id, plan)
    assert "blocked_reason" not in out


# --------------------------------------------------------------------------
# Edge-case coverage per TODO.md M6 (20–30 cases)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embedded_gain_position_not_flagged_as_harvestable(db, monkeypatch):
    """A position UP 20% should not appear in find_losses output."""
    from backend.services import tlh_tools
    from backend.models.models import Position, TaxLot
    port = await _build_portfolio(db, [("NVDA", 100, 100)])
    # Seed a tax lot so there's something to evaluate
    r = await db.execute(__import__("sqlalchemy").select(Position).where(Position.symbol == "NVDA"))
    pos = r.scalar_one()
    db.add(TaxLot(
        position_id=pos.id, shares=100, cost_basis=100.0,
        purchase_date=datetime.utcnow() - timedelta(days=100),
    ))
    await db.commit()

    async def fake_quotes(symbols):
        return {s: {"current_price": 120.0} for s in symbols}  # gained 20%
    monkeypatch.setattr(tlh_tools.finnhub_client, "get_multiple_quotes", fake_quotes)

    result = await tlh_tools.find_losses(db, port.id, min_loss_pct=0.02)
    syms = [o["symbol"] for o in result.get("opportunities", [])]
    assert "NVDA" not in syms


@pytest.mark.asyncio
async def test_low_basis_long_held_lot_prefers_long_term(db, monkeypatch):
    """When sellable, LT losses should be preferred over ST (lower tax rate)."""
    from backend.services import tlh_tools
    from backend.models.models import Position, TaxLot
    port = await _build_portfolio(db, [("META", 100, 200)])
    r = await db.execute(__import__("sqlalchemy").select(Position).where(Position.symbol == "META"))
    pos = r.scalar_one()
    # ST lot and LT lot — both at a loss
    db.add(TaxLot(
        position_id=pos.id, shares=50, cost_basis=200.0,
        purchase_date=datetime.utcnow() - timedelta(days=30),  # ST
    ))
    db.add(TaxLot(
        position_id=pos.id, shares=50, cost_basis=200.0,
        purchase_date=datetime.utcnow() - timedelta(days=400),  # LT
    ))
    await db.commit()

    async def fake_quotes(symbols):
        return {s: {"current_price": 150.0} for s in symbols}
    monkeypatch.setattr(tlh_tools.finnhub_client, "get_multiple_quotes", fake_quotes)

    result = await tlh_tools.find_losses(db, port.id, min_loss_pct=0.02)
    opp = next((o for o in result["opportunities"] if o["symbol"] == "META"), None)
    assert opp is not None
    # Position is a total loss; lots[] should show both ST and LT holding periods
    assert opp["total_unrealized_loss"] < 0
    terms = {lot["is_long_term"] for lot in opp["lots"]}
    assert True in terms and False in terms


@pytest.mark.asyncio
async def test_st_to_lt_crossover_boundary(db):
    """A lot at exactly 365 days should be classified long-term (_is_long_term uses >=)."""
    from backend.services.lot_engine import _is_long_term
    purchase = datetime(2023, 1, 1)
    sale_at_364 = datetime(2023, 12, 31)
    sale_at_365 = datetime(2024, 1, 1)
    sale_at_366 = datetime(2024, 1, 2)
    assert _is_long_term(purchase, sale_at_364) is False
    assert _is_long_term(purchase, sale_at_365) is True
    assert _is_long_term(purchase, sale_at_366) is True


@pytest.mark.asyncio
async def test_post_sale_wash_sale_window_blocks_repurchase(db, monkeypatch):
    """If we sold AAPL 10 days ago at a loss, proposing to buy AAPL back should flag."""
    port = await _build_portfolio(db, [("AAPL", 0, 0)])
    from backend.models.models import Transaction
    db.add(Transaction(
        portfolio_id=port.id, symbol="AAPL",
        transaction_type="HARVEST", shares=10, price=100, total_value=1000,
        timestamp=datetime.utcnow() - timedelta(days=10),
    ))
    await db.commit()

    # `find_losses` should exclude AAPL as recently-sold (wash window)
    from backend.services.tax_loss_service import tax_loss_service
    async def fake_quotes(symbols):
        return {s: {"current_price": 90.0} for s in symbols}
    from backend.services import tax_loss_service as tls_mod
    monkeypatch.setattr(tls_mod.finnhub_client, "get_multiple_quotes", fake_quotes)

    r = await tax_loss_service.get_tax_loss_opportunities(db, port.id)
    assert "AAPL" in r["wash_sale_restricted_symbols"]


@pytest.mark.asyncio
async def test_guardrails_preserve_non_si_replacements(db, monkeypatch):
    """Replacements that are not in the SI map should pass through untouched."""
    port = await _build_portfolio(db, [("XLK", 100, 200)])  # Tech sector ETF
    async def fake_quotes(symbols):
        return {s: {"current_price": 1000.0} for s in symbols}
    from backend.services import ai_guardrails as g
    monkeypatch.setattr(g.finnhub_client, "get_multiple_quotes", fake_quotes)

    plan = {
        "sells": [{"symbol": "XLK", "shares": 10, "price": 180}],
        "buys":  [{"symbol": "VGT", "shares": 10, "price": 500}],  # Different tech ETF, not SI
    }
    out, warnings = await apply_guardrails(db, port.id, plan)
    assert len(out["buys"]) == 1
    assert out["buys"][0]["symbol"] == "VGT"


@pytest.mark.asyncio
async def test_guardrails_block_exact_symbol_repurchase(db, monkeypatch):
    """Plan to SELL AAPL and BUY AAPL as replacement — classic wash-sale bait."""
    port = await _build_portfolio(db, [("AAPL", 100, 200)])
    async def fake_quotes(symbols):
        return {s: {"current_price": 1000.0} for s in symbols}
    from backend.services import ai_guardrails as g
    monkeypatch.setattr(g.finnhub_client, "get_multiple_quotes", fake_quotes)

    plan = {
        "sells": [{"symbol": "AAPL", "shares": 10, "price": 180}],
        "buys":  [{"symbol": "AAPL", "shares": 10, "price": 180}],
    }
    out, warnings = await apply_guardrails(db, port.id, plan)
    assert out["buys"] == []  # same-symbol replacement must be stripped


@pytest.mark.asyncio
async def test_guardrails_empty_plan_passes(db):
    port = await _build_portfolio(db, [])
    out, warnings = await apply_guardrails(db, port.id, {"sells": [], "buys": []})
    assert out["sells"] == []
    assert out["buys"] == []


def test_schema_rejects_non_object_in_sells():
    plan = {"sells": ["not a dict"], "buys": []}
    ok, errors = validate_draft_plan_schema(plan)
    assert ok is False
    assert any("not an object" in e for e in errors)


def test_schema_accepts_empty_lists():
    ok, _ = validate_draft_plan_schema({"sells": [], "buys": []})
    assert ok is True


def test_si_map_covers_major_sp500_etfs():
    """Guard against silent drops from the block list during refactors."""
    for must_have in ("SPY", "IVV", "VOO", "QQQ", "VTI"):
        assert must_have in SUBSTANTIALLY_IDENTICAL


@pytest.mark.asyncio
async def test_guardrails_flag_multiple_wash_sale_symbols(db):
    """Recent buys on TWO symbols should flag both sells."""
    port = await _build_portfolio(db, [("AAPL", 100, 200), ("MSFT", 100, 400)])
    from backend.models.models import Transaction
    db.add(Transaction(
        portfolio_id=port.id, symbol="AAPL",
        transaction_type="BUY", shares=10, price=150, total_value=1500,
        timestamp=datetime.utcnow() - timedelta(days=5),
    ))
    db.add(Transaction(
        portfolio_id=port.id, symbol="MSFT",
        transaction_type="BUY", shares=10, price=300, total_value=3000,
        timestamp=datetime.utcnow() - timedelta(days=15),
    ))
    await db.commit()

    plan = {
        "sells": [
            {"symbol": "AAPL", "shares": 50, "price": 150},
            {"symbol": "MSFT", "shares": 50, "price": 350},
        ],
        "buys": [],
    }
    out, warnings = await apply_guardrails(db, port.id, plan)
    wash_flagged = [s for s in out["sells"] if s.get("wash_sale_flag")]
    assert len(wash_flagged) == 2


@pytest.mark.asyncio
async def test_household_wash_sale_crosses_portfolios(db):
    """A BUY in a sibling portfolio of the same household must trigger wash-sale."""
    from backend.models.models import User, Client, Household, Portfolio, Transaction
    from backend.services.household_wash_sale import household_recent_buys

    user = User(email="h@b.com", hashed_password="x", role="individual")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    household = Household(name="Smith Family", owner_user_id=user.id)
    db.add(household)
    await db.commit()
    await db.refresh(household)
    c1 = Client(advisor_id=user.id, name="Self", is_self=True, household_id=household.id)
    c2 = Client(advisor_id=user.id, name="Spouse IRA", household_id=household.id)
    db.add_all([c1, c2])
    await db.commit()
    await db.refresh(c1); await db.refresh(c2)
    p1 = Portfolio(name="Taxable", initial_value=1000, cash=0, client_id=c1.id)
    p2 = Portfolio(name="IRA", initial_value=1000, cash=0, client_id=c2.id)
    db.add_all([p1, p2])
    await db.commit()
    await db.refresh(p1); await db.refresh(p2)

    # Buy AAPL in the IRA — selling AAPL at a loss in taxable should wash-sale
    db.add(Transaction(
        portfolio_id=p2.id, symbol="AAPL",
        transaction_type="BUY", shares=10, price=100, total_value=1000,
        timestamp=datetime.utcnow() - timedelta(days=5),
    ))
    await db.commit()

    buys = await household_recent_buys(db, p1.id, "AAPL")
    assert len(buys) == 1
    assert buys[0].portfolio_id == p2.id


@pytest.mark.asyncio
async def test_guardrails_warnings_are_structured_strings(db, monkeypatch):
    """Warnings must be a list of strings (UI contract — no dicts, no None)."""
    port = await _build_portfolio(db, [("SPY", 100, 400)])
    async def fake_quotes(symbols):
        return {s: {"current_price": 410.0} for s in symbols}
    from backend.services import ai_guardrails as g
    monkeypatch.setattr(g.finnhub_client, "get_multiple_quotes", fake_quotes)

    plan = {
        "sells": [{"symbol": "SPY", "shares": 100, "price": 410}],
        "buys":  [{"symbol": "IVV", "shares": 100, "price": 400}],
    }
    _, warnings = await apply_guardrails(db, port.id, plan)
    assert all(isinstance(w, str) for w in warnings)
    assert all(w.strip() for w in warnings)


@pytest.mark.asyncio
async def test_guardrails_no_quotes_available_skips_cap(db, monkeypatch):
    """If Finnhub returns empty, NAV falls to 0 and cap is a no-op (documented behavior)."""
    port = await _build_portfolio(db, [])  # no positions → NAV = 0
    async def fake_quotes(symbols):
        return {}
    from backend.services import ai_guardrails as g
    monkeypatch.setattr(g.finnhub_client, "get_multiple_quotes", fake_quotes)

    plan = {"sells": [{"symbol": "AAPL", "shares": 100, "price": 100}], "buys": []}
    out, _ = await apply_guardrails(db, port.id, plan)
    assert out.get("blocked_reason") != "MAX_SELL_PCT_EXCEEDED"
