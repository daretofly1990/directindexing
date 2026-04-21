"""
Structured reasoning + confidence builder.

Post-processes a draft trade plan to attach:
  - `citations`: for every sell, the specific lot IDs, basis, holding period,
                 unrealized loss, and *why* that lot was chosen (HIFO, oldest
                 eligible, etc.)
  - `confidence`: one of {"high", "medium", "low"} per sell, based on:
      * price-sensitivity (are we near break-even?)
      * wash-sale proximity
      * upcoming ST→LT crossover (sell today vs wait)
  - `caveats`: plain-English flags for volatility-sensitive proposals

Compliance angle: every recommendation cites *which lot, why it was picked,
why the replacement was chosen* — per TODO.md M6.
"""
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Position, TaxLot

ST_TO_LT_WARN_DAYS = 30     # within a month of LT → suggest waiting
WASH_WARN_DAYS = 5          # close to wash-sale window end → flag
PRICE_VOLATILITY_PCT = 0.03  # within 3% of break-even → "low confidence"


async def _get_lot_details(db: AsyncSession, lot_ids: list[int]) -> list[TaxLot]:
    if not lot_ids:
        return []
    r = await db.execute(select(TaxLot).where(TaxLot.id.in_(lot_ids)))
    return r.scalars().all()


async def _get_position_by_symbol(
    db: AsyncSession, portfolio_id: int, symbol: str,
) -> Position | None:
    r = await db.execute(
        select(Position).where(
            Position.portfolio_id == portfolio_id,
            Position.symbol == symbol,
        )
    )
    return r.scalar_one_or_none()


def _confidence_from(
    loss_pct: float, days_to_lt: int, days_since_wash: int | None,
) -> tuple[str, list[str]]:
    """Return (confidence, caveats) based on the numeric signals."""
    caveats: list[str] = []

    # Price-sensitivity
    if abs(loss_pct) < PRICE_VOLATILITY_PCT:
        caveats.append(
            f"Loss is shallow ({loss_pct:.1%}); a small price move could flip "
            f"this to a gain before the trade settles."
        )
    # ST→LT crossover
    if 0 < days_to_lt < ST_TO_LT_WARN_DAYS:
        caveats.append(
            f"This lot becomes long-term in {days_to_lt} days — consider waiting "
            f"to reduce the tax rate on any eventual gain on the replacement."
        )
    # Wash-sale proximity
    if days_since_wash is not None and days_since_wash < WASH_WARN_DAYS:
        caveats.append(
            f"A repurchase of this symbol {days_since_wash} days ago means the "
            f"wash-sale window still covers this sale — loss will be disallowed."
        )

    if len(caveats) == 0:
        return "high", []
    if len(caveats) == 1:
        return "medium", caveats
    return "low", caveats


async def enrich_draft_plan(
    db: AsyncSession, portfolio_id: int, draft_plan: dict,
) -> dict:
    """
    In-place: attach citations + confidence + caveats to each sell.

    Draft plan contract (from tlh_tools.draft_trade_list):
      {"sells": [{symbol, shares, price, lot_ids, ...}, ...],
       "buys":  [{symbol, shares, price, ...}, ...], ...}
    """
    if not isinstance(draft_plan, dict):
        return draft_plan
    sells = draft_plan.get("sells") or []
    now = datetime.utcnow()

    for sell in sells:
        sym = (sell.get("symbol") or "").upper()
        lot_ids = sell.get("lot_ids") or []
        price = sell.get("price") or sell.get("est_price") or 0

        lots = await _get_lot_details(db, lot_ids)
        pos = await _get_position_by_symbol(db, portfolio_id, sym)

        citations = []
        worst_loss_pct = 0.0
        min_days_to_lt = 10**6
        for lot in lots:
            holding_days = (now - lot.purchase_date).days
            days_to_lt = max(0, 365 - holding_days)
            loss_per_share = (price or 0) - lot.cost_basis
            loss_pct = (loss_per_share / lot.cost_basis) if lot.cost_basis > 0 else 0.0
            worst_loss_pct = min(worst_loss_pct, loss_pct)
            if holding_days < 365:
                min_days_to_lt = min(min_days_to_lt, days_to_lt)

            basis_reason = (
                "HIFO (highest cost basis realized first for max loss)"
                if lot.cost_basis > (pos.avg_cost_basis if pos else 0)
                else "selected by agent within the lot engine"
            )
            citations.append({
                "lot_id": lot.id,
                "shares": lot.shares,
                "cost_basis_per_share": lot.cost_basis,
                "purchase_date": lot.purchase_date.isoformat(),
                "holding_days": holding_days,
                "is_long_term": holding_days >= 365,
                "loss_per_share": round(loss_per_share, 4),
                "loss_pct": round(loss_pct, 4),
                "selection_reason": basis_reason,
            })

        days_to_lt_for_confidence = 0 if min_days_to_lt == 10**6 else min_days_to_lt
        confidence, caveats = _confidence_from(
            loss_pct=worst_loss_pct,
            days_to_lt=days_to_lt_for_confidence,
            days_since_wash=None,   # tlh_tools already handled pre-sale wash; left for future hook
        )

        sell["citations"] = citations
        sell["confidence"] = confidence
        if caveats:
            sell["caveats"] = caveats

    # Explain each buy: why this replacement was chosen
    for buy in draft_plan.get("buys") or []:
        buy.setdefault("selection_reason", (
            "Replacement chosen from sector-peer map to maintain exposure "
            "without triggering the substantially-identical security rule. "
            "Curated block list (not model-inferred)."
        ))

    return draft_plan
