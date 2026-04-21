"""
Admin-facing metrics for the ops dashboard.

Cheap enough to compute on demand for the first few hundred users — if the
tables get large, cache results in the `system_flags` table or precompute
from the scheduler.
"""
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..models.models import (
    User, Client, Portfolio, Transaction, TradePlan, AuditEvent,
    RecommendationLog, Subscription,
)


async def collect(db: AsyncSession) -> dict:
    """Single round-trip: pull counts + rates for the admin dashboard."""
    now = datetime.utcnow()
    d1 = now - timedelta(days=1)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    # --- Users ---
    users_total = (await db.execute(select(func.count(User.id)))).scalar() or 0
    users_active = (await db.execute(
        select(func.count(User.id)).where(User.is_active == True)  # noqa: E712
    )).scalar() or 0
    users_individual = (await db.execute(
        select(func.count(User.id)).where(User.role == "individual")
    )).scalar() or 0
    users_new_7d = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= d7)
    )).scalar() or 0
    users_new_30d = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= d30)
    )).scalar() or 0
    users_email_verified = (await db.execute(
        select(func.count(User.id)).where(User.email_verified == True)  # noqa: E712
    )).scalar() or 0
    users_totp = (await db.execute(
        select(func.count(User.id)).where(User.totp_enabled == True)  # noqa: E712
    )).scalar() or 0

    # --- Portfolios ---
    portfolios_total = (await db.execute(select(func.count(Portfolio.id)))).scalar() or 0

    # --- Trade plans ---
    plans_drafted = (await db.execute(
        select(func.count(TradePlan.id)).where(TradePlan.status == "DRAFT")
    )).scalar() or 0
    plans_approved = (await db.execute(
        select(func.count(TradePlan.id)).where(TradePlan.status == "APPROVED")
    )).scalar() or 0
    plans_executed = (await db.execute(
        select(func.count(TradePlan.id)).where(TradePlan.status == "EXECUTED")
    )).scalar() or 0
    plans_exec_30d = (await db.execute(
        select(func.count(TradePlan.id)).where(
            TradePlan.status == "EXECUTED",
            TradePlan.executed_at >= d30,
        )
    )).scalar() or 0

    # --- Transactions (harvest + sell activity) ---
    harvest_total = (await db.execute(
        select(func.count(Transaction.id)).where(Transaction.transaction_type == "HARVEST")
    )).scalar() or 0
    harvest_30d = (await db.execute(
        select(func.count(Transaction.id)).where(
            Transaction.transaction_type == "HARVEST",
            Transaction.timestamp >= d30,
        )
    )).scalar() or 0

    # --- AI recommendations ---
    recs_total = (await db.execute(select(func.count(RecommendationLog.id)))).scalar() or 0
    recs_24h = (await db.execute(
        select(func.count(RecommendationLog.id)).where(RecommendationLog.created_at >= d1)
    )).scalar() or 0

    # --- Subscriptions (MRR-ish) ---
    # MRR is approximate because we only look at tier + cycle; canonical number
    # is in the Stripe dashboard. Annual plans are divided by 12.
    sub_rows = (await db.execute(
        select(Subscription.tier, Subscription.billing_cycle, Subscription.status)
    )).all()
    tier_price_monthly = {"starter": 29.0, "standard": 59.0, "premium": 99.0}
    tier_price_annual = {"starter": 290.0, "standard": 590.0, "premium": 990.0}
    mrr = 0.0
    subs_active = 0
    subs_trialing = 0
    subs_past_due = 0
    for tier, cycle, status in sub_rows:
        if status == "trialing":
            subs_trialing += 1
        elif status == "active":
            subs_active += 1
            if cycle == "annual":
                mrr += tier_price_annual.get(tier, 0) / 12.0
            else:
                mrr += tier_price_monthly.get(tier, 0)
        elif status == "past_due":
            subs_past_due += 1

    # --- Audit event rate (crude health signal) ---
    audit_24h = (await db.execute(
        select(func.count(AuditEvent.id)).where(AuditEvent.created_at >= d1)
    )).scalar() or 0
    audit_7d = (await db.execute(
        select(func.count(AuditEvent.id)).where(AuditEvent.created_at >= d7)
    )).scalar() or 0

    return {
        "generated_at": now.isoformat(),
        "users": {
            "total": users_total,
            "active": users_active,
            "individual": users_individual,
            "new_7d": users_new_7d,
            "new_30d": users_new_30d,
            "email_verified": users_email_verified,
            "totp_enabled": users_totp,
        },
        "portfolios": {
            "total": portfolios_total,
        },
        "trade_plans": {
            "draft": plans_drafted,
            "approved": plans_approved,
            "executed": plans_executed,
            "executed_30d": plans_exec_30d,
        },
        "harvests": {
            "total": harvest_total,
            "last_30d": harvest_30d,
        },
        "ai": {
            "recommendations_total": recs_total,
            "recommendations_24h": recs_24h,
        },
        "subscriptions": {
            "active": subs_active,
            "trialing": subs_trialing,
            "past_due": subs_past_due,
            "mrr_estimate": round(mrr, 2),
        },
        "audit": {
            "events_24h": audit_24h,
            "events_7d": audit_7d,
        },
    }
