"""
Invite your CPA — magic-link tax report sharing.

Two router groups in this file:

  - `/cpa-invites/...` — authenticated user actions (create, list, revoke).
    Mounted under the global JWT-gated group in main.py.
  - `/cpa/view` — public read-only endpoint for the invited CPA. Mounted
    separately so the JWT dependency does NOT run. Token in the query
    string is the only auth.
"""
import logging
from datetime import datetime

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...database import get_db
from ...models.models import CPAInvite, Portfolio
from ...services.audit import log_audit
from ...services.cpa_invite_service import (
    build_cpa_view_payload,
    create_cpa_invite,
    record_view,
    resolve_invite,
    revoke_invite,
)

logger = logging.getLogger(__name__)

# User-facing (authenticated) endpoints
router = APIRouter(prefix="/cpa-invites", tags=["cpa-invites"])

# Public endpoint (no JWT). Mounted separately in main.py.
public_router = APIRouter(prefix="/cpa", tags=["cpa-invites"])


class CreateInviteRequest(BaseModel):
    portfolio_id: int
    cpa_email: EmailStr
    cpa_name: str | None = None
    firm_name: str | None = None


class InviteResponse(BaseModel):
    id: int
    portfolio_id: int
    cpa_email: str
    cpa_name: str | None
    firm_name: str | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    first_viewed_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int


def _to_response(invite: CPAInvite) -> InviteResponse:
    return InviteResponse(
        id=invite.id,
        portfolio_id=invite.portfolio_id,
        cpa_email=invite.cpa_email,
        cpa_name=invite.cpa_name,
        firm_name=invite.firm_name,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        revoked_at=invite.revoked_at,
        first_viewed_at=invite.first_viewed_at,
        last_viewed_at=invite.last_viewed_at,
        view_count=invite.view_count,
    )


async def _assert_own_portfolio(
    db: AsyncSession, user, portfolio_id: int,
) -> Portfolio:
    """Reuse the same access rules as assert_portfolio_access without the
    FastAPI dep plumbing."""
    from ...api.deps import assert_portfolio_access
    # assert_portfolio_access is a FastAPI dependency; reimplement inline to
    # avoid pulling in Request-scope state here.
    portfolio = await db.get(Portfolio, portfolio_id)
    if not portfolio:
        raise HTTPException(404, "Portfolio not found")
    if user.role == "admin":
        return portfolio
    if portfolio.client_id is None:
        raise HTTPException(403, "Access denied")
    from ...models.models import Client
    client = await db.get(Client, portfolio.client_id)
    if not client or client.advisor_id != user.id:
        raise HTTPException(403, "Access denied")
    if user.role == "individual" and not client.is_self:
        raise HTTPException(403, "Access denied")
    return portfolio


@router.post("", response_model=InviteResponse, status_code=201)
async def create_invite(
    req: CreateInviteRequest,
    request: Request,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a CPA invite for one of the user's own portfolios. Sends an email
    to `cpa_email` with a signed magic-link that expires in 30 days.
    """
    portfolio = await _assert_own_portfolio(db, current_user, req.portfolio_id)
    invite, _token = await create_cpa_invite(
        db,
        user=current_user,
        portfolio=portfolio,
        cpa_email=str(req.cpa_email),
        cpa_name=req.cpa_name,
        firm_name=req.firm_name,
    )
    await log_audit(
        db,
        event_type="CPA_INVITE_SENT",
        user_id=current_user.id,
        portfolio_id=portfolio.id,
        object_type="cpa_invite",
        object_id=invite.id,
        details={
            "cpa_email": invite.cpa_email,
            "firm_name": invite.firm_name,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(invite)
    return _to_response(invite)


@router.get("", response_model=list[InviteResponse])
async def list_invites(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the current user's active + historical CPA invites."""
    rows = (await db.execute(
        select(CPAInvite)
        .where(CPAInvite.user_id == current_user.id)
        .order_by(desc(CPAInvite.created_at))
    )).scalars().all()
    return [_to_response(r) for r in rows]


@router.delete("/{invite_id}", status_code=204)
async def delete_invite(
    invite_id: int,
    request: Request,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a CPA invite. The magic-link immediately starts returning 410."""
    invite = await db.get(CPAInvite, invite_id)
    if not invite or invite.user_id != current_user.id:
        raise HTTPException(404, "Invite not found")
    if invite.revoked_at is None:
        await revoke_invite(db, invite)
        await log_audit(
            db,
            event_type="CPA_INVITE_REVOKED",
            user_id=current_user.id,
            portfolio_id=invite.portfolio_id,
            object_type="cpa_invite",
            object_id=invite.id,
            ip_address=request.client.host if request.client else None,
        )
    await db.commit()
    return None


# --- Public magic-link endpoint (no JWT) ---------------------------------

@public_router.get("/view")
async def view_cpa_report(
    token: str = Query(..., min_length=20),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint used by the CPA. Verifies the magic-link token, returns a
    sanitized realized-gains payload for the linked portfolio.

    Responses:
      200 — payload returned
      401 — token invalid or signature bad
      410 — invite expired or revoked
      404 — invite record not found (deleted out-of-band)
    """
    try:
        invite, status = await resolve_invite(db, token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(410, "Invite expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    except LookupError as e:
        raise HTTPException(404, str(e))

    if status == "expired":
        raise HTTPException(410, "Invite expired")
    if status == "revoked":
        raise HTTPException(410, "Invite revoked")

    await record_view(db, invite)
    await log_audit(
        db,
        event_type="CPA_INVITE_VIEWED",
        user_id=invite.user_id,
        portfolio_id=invite.portfolio_id,
        object_type="cpa_invite",
        object_id=invite.id,
        details={
            "cpa_email": invite.cpa_email,
            "view_count": invite.view_count,
        },
        ip_address=request.client.host if request and request.client else None,
    )
    payload = await build_cpa_view_payload(db, invite)
    await db.commit()
    return payload
