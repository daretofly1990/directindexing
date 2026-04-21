"""
Nightly scan: email individual users when their portfolio has meaningful
harvestable losses. Retention mechanic for the subscription — "the software
earned its fee today."

Rate limiting:
  - Min $500 of harvestable loss to trigger (configurable)
  - At most one harvest email per user per 7 days (checked via audit events)
  - Users can opt out with a HARVEST_NOTIFICATIONS_DISABLED audit event

Opt-out endpoint: POST /api/users/me/notifications {harvest_opportunities: bool}
Opt-in is the default. No DB migration — state lives in AuditEvent history.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..config import settings
from ..models.models import AuditEvent, Client, Portfolio, User
from .audit import log_audit
from .email_service import send_email
from .tax_loss_service import tax_loss_service

logger = logging.getLogger(__name__)

MIN_LOSS_TO_NOTIFY = 500.0      # dollars
NOTIFY_COOLDOWN_DAYS = 7
OPT_OUT_EVENT = "HARVEST_NOTIFICATIONS_DISABLED"
OPT_IN_EVENT = "HARVEST_NOTIFICATIONS_ENABLED"
NOTIFY_SENT_EVENT = "HARVEST_NOTIFICATION_SENT"


async def _user_is_opted_in(db: AsyncSession, user_id: int) -> bool:
    """Most recent opt-in/opt-out event wins. Default = opted in."""
    r = await db.execute(
        select(AuditEvent).where(
            AuditEvent.user_id == user_id,
            AuditEvent.event_type.in_([OPT_IN_EVENT, OPT_OUT_EVENT]),
        ).order_by(AuditEvent.created_at.desc()).limit(1)
    )
    ev = r.scalar_one_or_none()
    if ev is None:
        return True
    return ev.event_type == OPT_IN_EVENT


async def _last_notify_within(db: AsyncSession, user_id: int, days: int) -> bool:
    cutoff = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(AuditEvent.id).where(
            AuditEvent.user_id == user_id,
            AuditEvent.event_type == NOTIFY_SENT_EVENT,
            AuditEvent.created_at >= cutoff,
        ).limit(1)
    )
    return r.first() is not None


def _render_email(user_email: str, opportunities: list[dict], total_loss: float,
                  savings: float, unsubscribe_url: str) -> tuple[str, str]:
    """Return (text, html)."""
    top = sorted(opportunities, key=lambda o: o.get("total_unrealized_loss", 0))[:5]
    top_lines = [
        f"  {o['symbol']:6s} {o.get('position_loss_pct', 0):+.1f}%   "
        f"${abs(o.get('total_unrealized_loss', 0)):,.0f} loss"
        for o in top
    ]
    text = (
        f"We scanned your portfolio and found ${abs(total_loss):,.0f} of harvestable tax losses.\n\n"
        f"At a 37% marginal bracket, that's roughly ${savings:,.0f} in tax savings if you harvest before year-end.\n\n"
        f"Top opportunities:\n" + "\n".join(top_lines) + "\n\n"
        f"Open the AI Advisor tab to review: {settings.APP_BASE_URL}/?tab=advisor\n\n"
        f"—\n"
        f"To stop these notifications: {unsubscribe_url}\n"
    )
    rows = "".join(
        f"<tr><td style='padding:6px 10px;font-family:monospace;'>{o['symbol']}</td>"
        f"<td style='padding:6px 10px;text-align:right;color:#dc2626;'>{o.get('position_loss_pct', 0):+.1f}%</td>"
        f"<td style='padding:6px 10px;text-align:right;'>${abs(o.get('total_unrealized_loss', 0)):,.0f}</td></tr>"
        for o in top
    )
    html = f"""\
<!doctype html>
<html><body style="font-family:Arial,sans-serif;color:#1f2937;background:#f9fafb;margin:0;padding:24px;">
  <table role="presentation" style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:28px;">
    <tr><td>
      <div style="font-weight:700;font-size:18px;margin-bottom:6px;">DirectIndex Pro</div>
      <div style="color:#6b7280;font-size:13px;margin-bottom:18px;">Tax-loss harvesting opportunities</div>
      <div style="font-size:24px;font-weight:800;color:#059669;margin-bottom:4px;">${savings:,.0f}</div>
      <div style="font-size:13px;color:#6b7280;margin-bottom:18px;">
        estimated tax savings if you harvest <strong>${abs(total_loss):,.0f}</strong> of losses (37% bracket).
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:22px;">
        <thead>
          <tr style="background:#f3f4f6;">
            <th style="padding:8px 10px;text-align:left;">Symbol</th>
            <th style="padding:8px 10px;text-align:right;">Loss %</th>
            <th style="padding:8px 10px;text-align:right;">Unrealized $</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <a href="{settings.APP_BASE_URL}" style="display:inline-block;background:#3b82f6;color:#fff;padding:12px 22px;border-radius:6px;font-weight:600;text-decoration:none;">
        Open the AI Advisor
      </a>
      <p style="font-size:11px;color:#9ca3af;margin-top:22px;">
        No trades have been executed. The advisor drafts plans for your review.
        This is not tax or investment advice — see your disclosure documents.<br>
        <a href="{unsubscribe_url}" style="color:#6b7280;">Unsubscribe from these notifications</a>
      </p>
    </td></tr>
  </table>
</body></html>"""
    return text, html


async def scan_and_notify_one(db: AsyncSession, user: User, portfolio: Portfolio) -> dict:
    """Check a single portfolio; email if the threshold is met and not on cooldown."""
    if not await _user_is_opted_in(db, user.id):
        return {"status": "skipped", "reason": "opted_out", "user_id": user.id}
    if await _last_notify_within(db, user.id, NOTIFY_COOLDOWN_DAYS):
        return {"status": "skipped", "reason": "cooldown", "user_id": user.id}

    report = await tax_loss_service.get_tax_loss_opportunities(db, portfolio.id)
    total_loss = report.get("total_harvestable_loss", 0.0)
    if abs(total_loss) < MIN_LOSS_TO_NOTIFY:
        return {"status": "skipped", "reason": "below_threshold",
                "user_id": user.id, "total_loss": total_loss}

    savings = report.get("estimated_total_tax_savings") or (abs(total_loss) * 0.37)
    # Simple unsubscribe — the link points at the app; the opt-out endpoint
    # handles the actual state flip after the user confirms in-session.
    unsubscribe_url = f"{settings.APP_BASE_URL}/?settings=notifications"

    text, html = _render_email(
        user.email, report.get("opportunities", []), total_loss, savings, unsubscribe_url,
    )
    try:
        await send_email(
            to=user.email,
            subject=f"Found ${abs(total_loss):,.0f} of tax-loss harvesting opportunities",
            text=text, html=html,
        )
    except Exception as exc:
        logger.warning("Harvest email send failed for user %s: %s", user.id, exc)
        return {"status": "error", "reason": str(exc), "user_id": user.id}

    await log_audit(
        db, event_type=NOTIFY_SENT_EVENT,
        user_id=user.id, portfolio_id=portfolio.id,
        details={
            "total_loss": total_loss,
            "estimated_savings": savings,
            "opportunity_count": len(report.get("opportunities", [])),
        },
    )
    await db.commit()
    return {"status": "sent", "user_id": user.id, "total_loss": total_loss,
            "estimated_savings": savings}


async def scan_and_notify_all(db: AsyncSession) -> dict:
    """Walk every individual-persona portfolio and send where appropriate."""
    r = await db.execute(
        select(User, Client, Portfolio)
        .join(Client, Client.advisor_id == User.id)
        .join(Portfolio, Portfolio.client_id == Client.id)
        .where(
            User.role == "individual",
            User.is_active == True,   # noqa: E712
            Client.is_self == True,   # noqa: E712
        )
    )
    sent = 0
    skipped = 0
    errors = 0
    per_user = []
    for user, _client, portfolio in r.all():
        try:
            result = await scan_and_notify_one(db, user, portfolio)
        except Exception as exc:
            logger.exception("Harvest notify crashed for user %s", user.id)
            result = {"status": "error", "user_id": user.id, "reason": str(exc)}
        per_user.append(result)
        if result["status"] == "sent":
            sent += 1
        elif result["status"] == "error":
            errors += 1
        else:
            skipped += 1
    return {"sent": sent, "skipped": skipped, "errors": errors, "per_user": per_user}
