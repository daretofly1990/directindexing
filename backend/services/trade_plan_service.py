"""
TradePlan lifecycle: DRAFT -> APPROVED -> EXECUTED | CANCELLED | EXPIRED.

Draft plans are generated either manually via `draft_trade_list()` or by the AI
advisor. The individual reviews and approves in the UI; expiration is 24 hours
from creation because prices drift. Execution happens at the customer's own
broker — we reconcile fills by having the customer re-upload their lot CSV.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import (
    Portfolio, TradePlan, TradePlanItem, AuditEvent, RecommendationLog,
)

DRAFT_TTL_HOURS = 24


def _expires_at(from_: datetime | None = None) -> datetime:
    return (from_ or datetime.utcnow()) + timedelta(hours=DRAFT_TTL_HOURS)


async def create_trade_plan(
    db: AsyncSession,
    portfolio_id: int,
    draft_plan: dict,
    user_id: int,
    summary: str = "",
    recommendation_log_id: int | None = None,
) -> TradePlan:
    """
    Persist a draft plan. `draft_plan` is the dict returned by tlh_tools.draft_trade_list:
      {trades: [{action, symbol, shares, price, proceeds?, lot_ids?, notes?}, ...], ...}
    """
    portfolio = await db.get(Portfolio, portfolio_id)
    if not portfolio:
        raise ValueError("Portfolio not found")

    plan = TradePlan(
        portfolio_id=portfolio_id,
        status="DRAFT",
        draft_plan=json.dumps(draft_plan),
        summary=summary,
        created_by_user_id=user_id,
        expires_at=_expires_at(),
        recommendation_log_id=recommendation_log_id,
    )
    db.add(plan)
    await db.flush()

    # Accept either a flat `trades` list (action included) or separate sells/buys lists
    items: list[tuple[str, dict]] = []
    if draft_plan.get("trades"):
        for t in draft_plan["trades"]:
            action = (t.get("action") or t.get("type") or "").upper()
            if action in ("BUY", "SELL"):
                items.append((action, t))
    else:
        for t in draft_plan.get("sells") or []:
            items.append(("SELL", t))
        for t in draft_plan.get("buys") or []:
            items.append(("BUY", t))

    for action, t in items:
        item = TradePlanItem(
            plan_id=plan.id,
            action=action,
            symbol=(t.get("symbol") or "").upper(),
            shares=float(t.get("shares") or 0),
            est_price=t.get("price") or t.get("est_price"),
            est_proceeds=t.get("proceeds") or t.get("est_proceeds"),
            lot_ids_json=json.dumps(t["lot_ids"]) if t.get("lot_ids") else None,
            notes=t.get("notes"),
        )
        db.add(item)

    db.add(AuditEvent(
        user_id=user_id, event_type="TRADE_PLAN_CREATED", portfolio_id=portfolio_id,
        object_type="trade_plan", object_id=plan.id,
        details_json=json.dumps({"summary": summary, "item_count": len(items)}),
    ))
    await db.commit()
    await db.refresh(plan)
    return plan


async def approve_plan(db: AsyncSession, plan_id: int, user_id: int) -> TradePlan:
    plan = await db.get(TradePlan, plan_id)
    if not plan:
        raise ValueError("Plan not found")
    if plan.status != "DRAFT":
        raise ValueError(f"Cannot approve plan in status {plan.status}")
    if plan.expires_at and datetime.utcnow() > plan.expires_at:
        plan.status = "EXPIRED"
        await db.commit()
        raise ValueError("Plan has expired — draft a fresh one")
    plan.status = "APPROVED"
    plan.approved_by_user_id = user_id
    plan.approved_at = datetime.utcnow()
    db.add(AuditEvent(
        user_id=user_id, event_type="TRADE_PLAN_APPROVED", portfolio_id=plan.portfolio_id,
        object_type="trade_plan", object_id=plan.id,
    ))
    await db.commit()
    await db.refresh(plan)
    return plan


async def cancel_plan(db: AsyncSession, plan_id: int, user_id: int) -> TradePlan:
    plan = await db.get(TradePlan, plan_id)
    if not plan:
        raise ValueError("Plan not found")
    if plan.status in ("EXECUTED", "CANCELLED"):
        raise ValueError(f"Cannot cancel plan in status {plan.status}")
    plan.status = "CANCELLED"
    db.add(AuditEvent(
        user_id=user_id, event_type="TRADE_PLAN_CANCELLED", portfolio_id=plan.portfolio_id,
        object_type="trade_plan", object_id=plan.id,
    ))
    await db.commit()
    await db.refresh(plan)
    return plan


async def mark_executed(db: AsyncSession, plan_id: int, user_id: int, notes: str = "") -> TradePlan:
    plan = await db.get(TradePlan, plan_id)
    if not plan:
        raise ValueError("Plan not found")
    if plan.status != "APPROVED":
        raise ValueError(f"Plan must be APPROVED before marking executed (was {plan.status})")
    plan.status = "EXECUTED"
    plan.executed_at = datetime.utcnow()
    db.add(AuditEvent(
        user_id=user_id, event_type="TRADE_PLAN_EXECUTED", portfolio_id=plan.portfolio_id,
        object_type="trade_plan", object_id=plan.id,
        details_json=json.dumps({"notes": notes}),
    ))
    await db.commit()
    await db.refresh(plan)
    return plan


async def list_plans(db: AsyncSession, portfolio_id: int) -> list[TradePlan]:
    result = await db.execute(
        select(TradePlan).where(TradePlan.portfolio_id == portfolio_id)
        .order_by(TradePlan.created_at.desc())
    )
    return result.scalars().all()


def plan_to_dict(plan: TradePlan) -> dict:
    return {
        "id": plan.id,
        "portfolio_id": plan.portfolio_id,
        "status": plan.status,
        "summary": plan.summary or "",
        "created_at": plan.created_at.isoformat(),
        "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
        "executed_at": plan.executed_at.isoformat() if plan.executed_at else None,
        "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
        "draft_plan": json.loads(plan.draft_plan) if plan.draft_plan else None,
        "items": [
            {
                "id": i.id, "action": i.action, "symbol": i.symbol, "shares": i.shares,
                "est_price": i.est_price, "est_proceeds": i.est_proceeds,
                "lot_ids": json.loads(i.lot_ids_json) if i.lot_ids_json else None,
                "notes": i.notes,
            }
            for i in plan.items
        ],
    }
