"""
"Invite your CPA" — magic-link tax report sharing.

Flow:

  1. Individual user calls POST /api/cpa-invites with cpa_email +
     portfolio_id. We create a CPAInvite row, mint a signed JWT (30-day
     TTL), email the CPA a link to /cpa/view?token=<jwt>.
  2. CPA clicks the link. Backend verifies the token, looks up the invite,
     checks it's not expired or revoked, bumps view_count, emits a
     CPA_INVITE_VIEWED audit event, returns a sanitized JSON blob of the
     portfolio's realized gains + Form 8949 rows + summary. No auth.
  3. User can revoke the invite at any time with DELETE /api/cpa-invites/{id}.

Security boundary:
  - Token is a JWT signed with `JWT_SECRET`. `jti` claim is SHA-256-hashed
    before storage (so a DB leak does not let an attacker replay the raw
    token; they'd need the JWT itself).
  - The magic-link endpoint exposes ONLY the realized-gains data for that
    one portfolio — no position names, no user PII beyond what's on the
    public Schedule D anyway (cost basis, proceeds, dates, symbols).
  - We use `purpose=cpa_view` in the JWT to prevent token reuse as an
    auth token elsewhere.
  - Revoked or expired invites return 410 Gone.

The underlying query is the same one the `/tax-report.csv` endpoint runs,
so what the CPA sees is always in sync with what the account-holder sees.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.models import CPAInvite, Portfolio, Position, TaxLot
from .email_service import send_email

logger = logging.getLogger(__name__)

INVITE_TTL = timedelta(days=30)
INVITE_PURPOSE = "cpa_view"


def _hash_jti(jti: str) -> str:
    return hashlib.sha256(jti.encode()).hexdigest()


def _mint_token(invite_id: int, ttl: timedelta = INVITE_TTL) -> tuple[str, str]:
    """Return (jwt, jti_hash). jti_hash is stored; raw jwt goes in the email."""
    jti = secrets.token_urlsafe(24)
    token = jwt.encode(
        {
            "sub": str(invite_id),
            "purpose": INVITE_PURPOSE,
            "jti": jti,
            "exp": datetime.utcnow() + ttl,
        },
        settings.JWT_SECRET,
        algorithm="HS256",
    )
    return token, _hash_jti(jti)


def _decode_token(token: str) -> tuple[int, str]:
    """Return (invite_id, jti_hash). Raises jwt.* on bad/expired tokens."""
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    if payload.get("purpose") != INVITE_PURPOSE:
        raise jwt.InvalidTokenError("token purpose mismatch")
    jti = payload.get("jti")
    if not jti:
        raise jwt.InvalidTokenError("missing jti")
    return int(payload["sub"]), _hash_jti(jti)


def _render_cpa_email_html(user_name: str, firm: str | None, link: str) -> str:
    firm_line = f"<div style='color:#6b7280;font-size:13px;margin-bottom:20px;'>For: {firm}</div>" if firm else ""
    return f"""\
<!doctype html>
<html><body style="font-family:Arial,sans-serif;color:#1f2937;background:#f9fafb;margin:0;padding:24px;">
  <table role="presentation" style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:28px;">
    <tr><td>
      <div style="font-weight:700;font-size:18px;margin-bottom:6px;">DirectIndex Pro — tax report access</div>
      {firm_line}
      <p style="font-size:15px;line-height:1.6;margin:0 0 18px;">
        {user_name} has shared their DirectIndex Pro realized-gains report with you. The link
        gives read-only access to realized gains, Form 8949 rows, and a per-lot tax summary
        for this tax year. No account access, no write permission.
      </p>
      <a href="{link}" style="display:inline-block;background:#3b82f6;color:#fff;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;">
        Open tax report
      </a>
      <p style="font-size:12px;color:#6b7280;margin-top:20px;">
        Link expires in 30 days. If you did not expect this, you can ignore it —
        the account holder can revoke the link at any time.
      </p>
    </td></tr>
  </table>
</body></html>"""


async def create_cpa_invite(
    db: AsyncSession,
    *,
    user,
    portfolio: Portfolio,
    cpa_email: str,
    cpa_name: str | None = None,
    firm_name: str | None = None,
    ttl: timedelta = INVITE_TTL,
    send: bool = True,
) -> tuple[CPAInvite, str]:
    """
    Create the CPAInvite row, mint the token, optionally send the email.
    Returns (invite_row, raw_token).
    """
    expires_at = datetime.utcnow() + ttl
    invite = CPAInvite(
        user_id=user.id,
        portfolio_id=portfolio.id,
        cpa_email=cpa_email.strip().lower(),
        cpa_name=cpa_name,
        firm_name=firm_name,
        # Populated below after we know the invite id (so jti is row-scoped)
        token_hash="",
        expires_at=expires_at,
    )
    db.add(invite)
    await db.flush()

    token, jti_hash = _mint_token(invite.id, ttl=ttl)
    invite.token_hash = jti_hash
    await db.flush()

    if send:
        link = f"{settings.APP_BASE_URL}/cpa/view?token={token}"
        sender_name = user.full_name or user.email
        text = (
            f"{sender_name} has shared their DirectIndex Pro realized-gains report with you.\n\n"
            f"Open the report:\n{link}\n\n"
            f"The link expires in 30 days and can be revoked at any time by the account holder.\n"
        )
        html = _render_cpa_email_html(sender_name, firm_name, link)
        try:
            await send_email(
                to=cpa_email,
                subject=f"Tax report access — DirectIndex Pro ({sender_name})",
                text=text, html=html,
            )
        except Exception as e:
            logger.error("CPA invite email failed: %s", e)

    return invite, token


async def resolve_invite(
    db: AsyncSession, token: str,
) -> tuple[CPAInvite, str]:
    """
    Look up an invite from a raw token. Raises the appropriate error via the
    caller's exception handler (we don't import FastAPI here to keep the
    service testable).

    Returns (invite, status) where status is "ok", "expired", or "revoked".
    """
    invite_id, jti_hash = _decode_token(token)
    invite = await db.get(CPAInvite, invite_id)
    if not invite:
        raise LookupError("invite not found")
    if invite.token_hash != jti_hash:
        # Token was issued for this invite_id but a different jti —
        # means it was rotated/replaced.
        raise LookupError("invite token mismatch")
    if invite.revoked_at is not None:
        return invite, "revoked"
    if invite.expires_at < datetime.utcnow():
        return invite, "expired"
    return invite, "ok"


async def record_view(db: AsyncSession, invite: CPAInvite) -> None:
    """Bump counters when a CPA opens the link."""
    now = datetime.utcnow()
    if invite.first_viewed_at is None:
        invite.first_viewed_at = now
    invite.last_viewed_at = now
    invite.view_count = (invite.view_count or 0) + 1
    await db.flush()


async def revoke_invite(db: AsyncSession, invite: CPAInvite) -> None:
    invite.revoked_at = datetime.utcnow()
    await db.flush()


async def build_cpa_view_payload(
    db: AsyncSession, invite: CPAInvite,
) -> dict:
    """
    The JSON served at /cpa/view. Realized-gain summary + per-lot rows
    scoped to the one portfolio. No position objects or user PII.
    """
    # Per-lot closed rows with symbol
    rows = (await db.execute(
        select(TaxLot, Position.symbol)
        .join(Position, TaxLot.position_id == Position.id)
        .where(
            Position.portfolio_id == invite.portfolio_id,
            TaxLot.sale_date.isnot(None),
        )
        .order_by(TaxLot.sale_date)
    )).all()

    lot_items: list[dict] = []
    total_proceeds = 0.0
    total_cost_basis = 0.0
    total_gain_short = 0.0
    total_gain_long = 0.0
    total_wash_disallowed = 0.0

    for lot, symbol in rows:
        holding_days = (lot.sale_date - lot.purchase_date).days if lot.sale_date else 0
        is_lt = holding_days >= 365
        cost_basis_total = round(lot.cost_basis * lot.shares, 2)
        proceeds = round(lot.proceeds or 0.0, 2)
        gain = round(lot.realized_gain_loss or 0.0, 2)
        wash = round(lot.wash_sale_disallowed or 0.0, 2)

        total_proceeds += proceeds
        total_cost_basis += cost_basis_total
        total_wash_disallowed += wash
        if is_lt:
            total_gain_long += gain
        else:
            total_gain_short += gain

        lot_items.append({
            "description": f"{lot.shares:g} shares {symbol}",
            "symbol": symbol,
            "shares": round(lot.shares, 6),
            "date_acquired": lot.purchase_date.strftime("%m/%d/%Y"),
            "date_sold": lot.sale_date.strftime("%m/%d/%Y"),
            "proceeds": proceeds,
            "cost_basis": cost_basis_total,
            "wash_sale_disallowed": wash,
            "gain_loss": gain,
            "term": "long" if is_lt else "short",
            "wash_sale_code": "W" if wash > 0 else "",
        })

    return {
        "portfolio_id": invite.portfolio_id,
        "as_of": datetime.utcnow().isoformat(),
        "cpa": {
            "email": invite.cpa_email,
            "name": invite.cpa_name,
            "firm": invite.firm_name,
        },
        "summary": {
            "total_proceeds": round(total_proceeds, 2),
            "total_cost_basis": round(total_cost_basis, 2),
            "total_realized_gain_short_term": round(total_gain_short, 2),
            "total_realized_gain_long_term": round(total_gain_long, 2),
            "total_wash_sale_disallowed": round(total_wash_disallowed, 2),
            "closed_lot_count": len(lot_items),
        },
        "closed_lots": lot_items,
        "expires_at": invite.expires_at.isoformat(),
        "disclosure": (
            "Realized gains shown are informational and not tax advice. "
            "Wash-sale adjustments may require additional reconciliation against "
            "broker 1099-B statements."
        ),
    }
