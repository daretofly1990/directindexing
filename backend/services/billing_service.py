"""
Stripe billing wrapper.

Tiers (matches pricing strategy doc):
  starter   — $29/mo or $290/yr   — portfolio value up to $100K
  standard  — $59/mo or $590/yr   — up to $500K
  premium   — $99/mo or $990/yr   — unlimited

Dev mode: when STRIPE_SECRET_KEY is empty, `stripe` is not imported and the
billing endpoints return 503. Wire your real keys in .env before enabling.

The webhook handler verifies signatures via STRIPE_WEBHOOK_SECRET. Events we
persist: customer.subscription.{created,updated,deleted}, invoice.paid,
invoice.payment_failed (dunning start).
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..config import settings
from ..models.models import Subscription, User

logger = logging.getLogger(__name__)

TIER_PRICE_MAP = {
    ("starter", "monthly"):  "STRIPE_PRICE_STARTER_MONTHLY",
    ("starter", "annual"):   "STRIPE_PRICE_STARTER_ANNUAL",
    ("standard", "monthly"): "STRIPE_PRICE_STANDARD_MONTHLY",
    ("standard", "annual"):  "STRIPE_PRICE_STANDARD_ANNUAL",
    ("premium", "monthly"):  "STRIPE_PRICE_PREMIUM_MONTHLY",
    ("premium", "annual"):   "STRIPE_PRICE_PREMIUM_ANNUAL",
}


def billing_enabled() -> bool:
    return bool(settings.STRIPE_SECRET_KEY)


def _get_price_id(tier: str, billing_cycle: str) -> str:
    key = TIER_PRICE_MAP.get((tier, billing_cycle))
    if not key:
        raise ValueError(f"Unknown tier/cycle: {tier}/{billing_cycle}")
    return getattr(settings, key, "") or ""


def _stripe():
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


async def get_or_create_customer(db: AsyncSession, user: User) -> str:
    r = await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    sub = r.scalar_one_or_none()
    if sub and sub.stripe_customer_id:
        return sub.stripe_customer_id
    stripe = _stripe()
    cust = stripe.Customer.create(email=user.email, name=user.full_name or None,
                                   metadata={"user_id": str(user.id)})
    if sub is None:
        sub = Subscription(user_id=user.id, stripe_customer_id=cust["id"], tier="starter")
        db.add(sub)
    else:
        sub.stripe_customer_id = cust["id"]
    await db.commit()
    return cust["id"]


async def create_checkout_session(
    db: AsyncSession, user: User, tier: str, billing_cycle: str, success_url: str, cancel_url: str,
) -> dict:
    if not billing_enabled():
        raise RuntimeError("Stripe is not configured (STRIPE_SECRET_KEY unset)")
    price_id = _get_price_id(tier, billing_cycle)
    if not price_id:
        raise ValueError(f"Price ID for {tier}/{billing_cycle} is not configured")

    stripe = _stripe()
    customer_id = await get_or_create_customer(db, user)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        subscription_data={"trial_period_days": settings.STRIPE_TRIAL_DAYS},
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
        metadata={"user_id": str(user.id), "tier": tier, "billing_cycle": billing_cycle},
    )
    return {"session_id": session["id"], "url": session["url"]}


async def create_portal_session(
    db: AsyncSession, user: User, return_url: str,
) -> dict:
    if not billing_enabled():
        raise RuntimeError("Stripe is not configured (STRIPE_SECRET_KEY unset)")
    customer_id = await get_or_create_customer(db, user)
    stripe = _stripe()
    session = stripe.billing_portal.Session.create(
        customer=customer_id, return_url=return_url,
    )
    return {"url": session["url"]}


async def get_subscription(db: AsyncSession, user_id: int) -> Subscription | None:
    r = await db.execute(select(Subscription).where(Subscription.user_id == user_id))
    return r.scalar_one_or_none()


async def get_active_tier(db: AsyncSession, user_id: int) -> str:
    """
    Return the user's current billable tier: one of
      "premium" | "standard" | "starter" | "default"

    "default" covers the cases where there's no active subscription (not yet
    set up, trial lapsed, cancelled, past due). Admin users also return
    "default" — they don't pay, but they should get DEFAULT model too unless
    billing_enabled=False in which case nothing matters.
    """
    sub = await get_subscription(db, user_id)
    if sub is None:
        return "default"
    # Trialing counts as the purchased tier — user is experiencing what they're paying for
    if sub.status in ("active", "trialing") and sub.tier in ("premium", "standard", "starter"):
        return sub.tier
    return "default"


async def get_claude_model_for_user(db: AsyncSession, user_id: int) -> str:
    """
    Pick the Claude model for this user. Premium tier → CLAUDE_MODEL_PREMIUM,
    everyone else → CLAUDE_MODEL_DEFAULT. Trialing premium users get premium
    (so they feel the upgrade before being charged).
    """
    tier = await get_active_tier(db, user_id)
    if tier == "premium":
        return settings.CLAUDE_MODEL_PREMIUM
    return settings.CLAUDE_MODEL_DEFAULT


async def list_invoices(db: AsyncSession, user: User, limit: int = 20) -> list[dict]:
    """
    Pull invoice list for this user from Stripe. Returns PDF-downloadable URLs.
    Empty list if the user hasn't started checkout yet (no customer_id).
    """
    sub = await get_subscription(db, user.id)
    if not sub or not sub.stripe_customer_id:
        return []
    stripe = _stripe()
    resp = stripe.Invoice.list(customer=sub.stripe_customer_id, limit=limit)
    out = []
    for inv in resp["data"]:
        out.append({
            "id": inv["id"],
            "number": inv.get("number"),
            "status": inv.get("status"),
            "amount_paid": inv.get("amount_paid") or 0,
            "amount_due": inv.get("amount_due") or 0,
            "currency": inv.get("currency"),
            "created": datetime.utcfromtimestamp(inv["created"]).isoformat(),
            "invoice_pdf": inv.get("invoice_pdf"),
            "hosted_invoice_url": inv.get("hosted_invoice_url"),
        })
    return out


async def handle_webhook_event(db: AsyncSession, event: dict) -> None:
    """Apply a verified Stripe webhook event to our Subscription rows."""
    et = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    if et in ("customer.subscription.created", "customer.subscription.updated"):
        customer_id = data.get("customer")
        if not customer_id:
            return
        r = await db.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        sub = r.scalar_one_or_none()
        if sub is None:
            user_id = int(data.get("metadata", {}).get("user_id") or 0) or None
            if not user_id:
                return
            sub = Subscription(user_id=user_id, stripe_customer_id=customer_id, tier="starter")
            db.add(sub)
        sub.stripe_subscription_id = data.get("id")
        sub.status = data.get("status") or sub.status
        sub.cancel_at_period_end = bool(data.get("cancel_at_period_end"))
        cpe = data.get("current_period_end")
        if cpe:
            sub.current_period_end = datetime.utcfromtimestamp(cpe)
        te = data.get("trial_end")
        if te:
            sub.trial_ends_at = datetime.utcfromtimestamp(te)
        items = (data.get("items") or {}).get("data") or []
        if items:
            sub.tier = (items[0].get("metadata") or {}).get("tier") or sub.tier
            interval = ((items[0].get("price") or {}).get("recurring") or {}).get("interval")
            if interval == "month":
                sub.billing_cycle = "monthly"
            elif interval == "year":
                sub.billing_cycle = "annual"
        sub.updated_at = datetime.utcnow()
        await db.commit()

    elif et == "customer.subscription.deleted":
        r = await db.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == data.get("id"))
        )
        sub = r.scalar_one_or_none()
        if sub:
            sub.status = "canceled"
            sub.updated_at = datetime.utcnow()
            await db.commit()

    elif et == "invoice.payment_failed":
        customer_id = data.get("customer")
        r = await db.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        sub = r.scalar_one_or_none()
        if sub:
            sub.status = "past_due"
            sub.updated_at = datetime.utcnow()
            await db.commit()

    elif et == "invoice.paid":
        customer_id = data.get("customer")
        r = await db.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        sub = r.scalar_one_or_none()
        if sub and sub.status in ("past_due", "unpaid"):
            sub.status = "active"
            sub.updated_at = datetime.utcnow()
            await db.commit()

    else:
        logger.debug("Ignoring Stripe event: %s", et)
