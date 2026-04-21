"""
Stripe-backed billing endpoints.

POST /api/billing/checkout         — create a Stripe Checkout session
GET  /api/billing/status           — current subscription tier + status
POST /api/billing/portal           — open the Stripe customer portal
POST /api/billing/webhook          — Stripe webhook receiver (signed)

Dev mode: when STRIPE_SECRET_KEY is empty, all four return 503 with a clear
message. No silent no-ops.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...config import settings
from ...database import get_db
from ...services import billing_service
from ...services.audit import log_audit

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    tier: str   # starter | standard | premium
    billing_cycle: str  # monthly | annual
    success_url: str
    cancel_url: str


class PortalRequest(BaseModel):
    return_url: str


def _require_enabled() -> None:
    if not billing_service.billing_enabled():
        raise HTTPException(
            503,
            "Billing is not configured. Set STRIPE_SECRET_KEY in .env and restart.",
        )


@router.get("/status")
async def billing_status(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sub = await billing_service.get_subscription(db, current_user.id)
    if sub is None:
        return {
            "has_subscription": False,
            "billing_enabled": billing_service.billing_enabled(),
        }
    return {
        "has_subscription": True,
        "billing_enabled": billing_service.billing_enabled(),
        "tier": sub.tier,
        "billing_cycle": sub.billing_cycle,
        "status": sub.status,
        "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
        "cancel_at_period_end": sub.cancel_at_period_end,
    }


@router.post("/checkout")
async def create_checkout(
    req: CheckoutRequest,
    request: Request,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    if req.tier not in ("starter", "standard", "premium"):
        raise HTTPException(400, "tier must be starter | standard | premium")
    if req.billing_cycle not in ("monthly", "annual"):
        raise HTTPException(400, "billing_cycle must be monthly | annual")
    try:
        r = await billing_service.create_checkout_session(
            db, current_user, req.tier, req.billing_cycle,
            req.success_url, req.cancel_url,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    await log_audit(
        db, event_type="BILLING_CHECKOUT_STARTED",
        user_id=current_user.id,
        details={"tier": req.tier, "billing_cycle": req.billing_cycle},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return r


@router.get("/invoices")
async def list_invoices(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invoice history + signed PDF download URLs from Stripe."""
    _require_enabled()
    try:
        invoices = await billing_service.list_invoices(db, current_user)
    except Exception as exc:
        raise HTTPException(500, f"Stripe list failed: {exc}")
    return {"invoices": invoices}


@router.post("/portal")
async def open_portal(
    req: PortalRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    try:
        return await billing_service.create_portal_session(db, current_user, req.return_url)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Stripe webhook receiver. Verifies signatures with STRIPE_WEBHOOK_SECRET.

    This endpoint is intentionally NOT behind JWT auth — Stripe calls it
    directly. The signature check IS the auth.
    """
    if not billing_service.billing_enabled():
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.STRIPE_WEBHOOK_SECRET,
        )
    except Exception as exc:
        raise HTTPException(400, f"Webhook signature verification failed: {exc}")
    await billing_service.handle_webhook_event(db, event)
    return {"received": True}
