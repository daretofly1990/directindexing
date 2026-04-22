"""
Hard guardrails applied to every AI-generated draft trade plan.

These rules are enforced post-hoc — we do NOT trust the model to self-police:
  1. Substantially-identical: any replacement on the curated block-list is removed
  2. Wash-sale: any SELL for a symbol bought in the last 30 days gets flagged
  3. Max % per day: total SELL notional capped at MAX_SELL_PCT of portfolio NAV
  4. Schema validation: reject draft plans that don't match the expected shape
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Portfolio, Position, Transaction
from .finnhub_client import finnhub_client

logger = logging.getLogger(__name__)


# Bump when the prompt template changes — stored on every RecommendationLog
PROMPT_VERSION = "tlh-v1-2026-04-19"
MODEL_VERSION = "claude-opus-4-5"

# Max fraction of portfolio NAV that may be sold in a single plan
MAX_SELL_PCT = 0.30

# Substantially-identical block list. Curated — never model-inferred.
# Maps symbol -> set of symbols treated as substantially identical under IRS rules
# (same index tracked, same issuer family, etc.).
SUBSTANTIALLY_IDENTICAL: dict[str, set[str]] = {
    # S&P 500 ETFs — IRS has treated these as substantially identical in examiner guidance
    "SPY":  {"IVV", "VOO", "SPLG"},
    "IVV":  {"SPY", "VOO", "SPLG"},
    "VOO":  {"SPY", "IVV", "SPLG"},
    "SPLG": {"SPY", "IVV", "VOO"},
    # Total-market ETFs
    "VTI":  {"ITOT", "SCHB"},
    "ITOT": {"VTI", "SCHB"},
    "SCHB": {"VTI", "ITOT"},
    # NASDAQ-100
    "QQQ":  {"QQQM"},
    "QQQM": {"QQQ"},
    # Aggregate bond index
    "AGG":  {"BND", "SCHZ"},
    "BND":  {"AGG", "SCHZ"},
    "SCHZ": {"AGG", "BND"},
}


def validate_draft_plan_schema(plan: dict) -> tuple[bool, list[str]]:
    """Shape check on the agent's draft trade list."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return False, ["draft_plan must be an object"]
    sells = plan.get("sells")
    buys = plan.get("buys")
    if sells is not None and not isinstance(sells, list):
        errors.append("sells must be a list")
    if buys is not None and not isinstance(buys, list):
        errors.append("buys must be a list")
    for i, t in enumerate(sells or []):
        if not isinstance(t, dict):
            errors.append(f"sells[{i}] not an object"); continue
        if not t.get("symbol"):
            errors.append(f"sells[{i}] missing symbol")
        if t.get("shares") is None:
            errors.append(f"sells[{i}] missing shares")
    for i, t in enumerate(buys or []):
        if not isinstance(t, dict):
            errors.append(f"buys[{i}] not an object"); continue
        if not t.get("symbol"):
            errors.append(f"buys[{i}] missing symbol")
    return len(errors) == 0, errors


async def _symbols_sold_recently(db: AsyncSession, portfolio_id: int, days: int = 30) -> set[str]:
    window = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(Transaction.symbol).where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.transaction_type.in_(["SELL", "HARVEST"]),
            Transaction.timestamp >= window,
        )
    )
    return {row[0] for row in r.all() if row[0]}


async def _portfolio_nav(db: AsyncSession, portfolio_id: int) -> float:
    r = await db.execute(
        select(Position).where(
            Position.portfolio_id == portfolio_id,
            Position.is_active == True,  # noqa: E712
        )
    )
    positions = r.scalars().all()
    if not positions:
        return 0.0
    symbols = [p.symbol for p in positions]
    quotes = await finnhub_client.get_multiple_quotes(symbols)
    nav = 0.0
    for p in positions:
        q = quotes.get(p.symbol, {})
        price = q.get("current_price") or p.avg_cost_basis
        nav += p.shares * price
    port = await db.get(Portfolio, portfolio_id)
    if port:
        nav += port.cash or 0.0
    return nav


async def check_manual_sell_cap(
    db: AsyncSession, portfolio_id: int, sell_notional: float,
) -> tuple[bool, str, float]:
    """
    Return (within_cap, reason, nav). Used by the manual spec-ID sell route
    to block sales exceeding MAX_SELL_PCT of NAV per request. Same threshold
    as the AI guardrail so behavior is consistent across code paths.
    """
    nav = await _portfolio_nav(db, portfolio_id)
    if nav <= 0:
        return True, "NAV unknown — cap skipped", nav
    limit = nav * MAX_SELL_PCT
    if sell_notional > limit:
        return False, (
            f"Sale of ${sell_notional:,.0f} exceeds the {MAX_SELL_PCT:.0%} "
            f"single-request cap (${limit:,.0f} on NAV ${nav:,.0f})."
        ), nav
    return True, "ok", nav


async def apply_guardrails(
    db: AsyncSession, portfolio_id: int, draft_plan: dict,
) -> tuple[dict, list[str]]:
    """
    Mutate the plan in-place: strip substantially-identical replacements,
    flag wash-sale violations, cap SELL notional. Returns (plan, warnings).
    """
    warnings: list[str] = []
    if not isinstance(draft_plan, dict):
        return draft_plan, ["draft_plan not an object"]

    sells = draft_plan.get("sells") or []
    buys = draft_plan.get("buys") or []

    # 1. Substantially-identical filter
    sell_symbols = {s.get("symbol", "").upper() for s in sells}
    identical_for_any_sell: set[str] = set()
    for s in sell_symbols:
        identical_for_any_sell |= SUBSTANTIALLY_IDENTICAL.get(s, set())

    filtered_buys = []
    for b in buys:
        buy_sym = (b.get("symbol") or "").upper()
        if buy_sym in identical_for_any_sell or buy_sym in sell_symbols:
            warnings.append(
                f"GUARDRAIL_BLOCKED_SI: dropped replacement {buy_sym} "
                f"(substantially identical to a harvested position)"
            )
            continue
        filtered_buys.append(b)
    draft_plan["buys"] = filtered_buys

    # 2. Wash-sale pre-sale: flag SELLs of symbols bought in last 30 days
    r = await db.execute(
        select(Transaction.symbol).where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.transaction_type == "BUY",
            Transaction.timestamp >= datetime.utcnow() - timedelta(days=30),
        )
    )
    recent_buy_symbols = {row[0] for row in r.all() if row[0]}
    for s in sells:
        sym = (s.get("symbol") or "").upper()
        if sym in recent_buy_symbols:
            warnings.append(
                f"WASH_SALE_RISK: {sym} was purchased within the last 30 days — "
                f"loss will be disallowed on sale"
            )
            s["wash_sale_flag"] = True

    # 3. Max-sell cap
    nav = await _portfolio_nav(db, portfolio_id)
    if nav > 0:
        sell_notional = sum(
            (s.get("shares") or 0) * (s.get("price") or s.get("est_price") or 0)
            for s in sells
        )
        if sell_notional > nav * MAX_SELL_PCT:
            warnings.append(
                f"GUARDRAIL_MAX_SELL: plan proposes selling "
                f"${sell_notional:,.0f} ({sell_notional/nav:.0%} of NAV) — "
                f"exceeds {MAX_SELL_PCT:.0%} daily cap"
            )
            draft_plan["blocked_reason"] = "MAX_SELL_PCT_EXCEEDED"

    return draft_plan, warnings
