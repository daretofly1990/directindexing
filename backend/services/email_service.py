"""
Email delivery + verification token issuance.

Dev mode: when `SMTP_HOST` is empty, emails are logged to stdout instead of
sent — so local dev works without real SMTP. Tests see `SentEmail` records
via the in-memory hook.

Prod: `aiosmtplib` over STARTTLS. Same interface supports SendGrid / Postmark /
Mailgun / AWS SES / Gmail App Passwords — just change the host/port/user/pass.

See `.env.example` for provider-specific setup hints.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.models import User

logger = logging.getLogger(__name__)

VERIFY_TOKEN_TTL = timedelta(hours=24)

# Test / dev hook: in-memory capture of sent emails. Tests can inspect this
# without mocking the whole SMTP client.
@dataclass
class SentEmail:
    to: str
    subject: str
    text: str
    html: str | None = None
    sent_at: datetime = field(default_factory=datetime.utcnow)


_SENT: list[SentEmail] = []


def captured_emails() -> list[SentEmail]:
    return list(_SENT)


def clear_captured_emails() -> None:
    _SENT.clear()


def _make_token(user_id: int, purpose: str = "verify_email") -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "purpose": purpose,
            "exp": datetime.utcnow() + VERIFY_TOKEN_TTL,
        },
        settings.JWT_SECRET,
        algorithm="HS256",
    )


def _decode_token(token: str, expected_purpose: str = "verify_email") -> int:
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    if payload.get("purpose") != expected_purpose:
        raise jwt.InvalidTokenError("token purpose mismatch")
    return int(payload["sub"])


def _render_verification_html(verify_url: str) -> str:
    return f"""\
<!doctype html>
<html><body style="font-family:Arial,sans-serif;color:#1f2937;background:#f9fafb;margin:0;padding:24px;">
  <table role="presentation" style="max-width:520px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:28px;">
    <tr><td>
      <div style="font-weight:700;font-size:18px;margin-bottom:6px;">DirectIndex Pro</div>
      <div style="color:#6b7280;font-size:13px;margin-bottom:20px;">Verify your email</div>
      <p style="font-size:15px;line-height:1.6;margin:0 0 18px;">
        Welcome. Click the button below to confirm your email. The link expires in 24 hours.
      </p>
      <a href="{verify_url}" style="display:inline-block;background:#3b82f6;color:#fff;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;">
        Verify email
      </a>
      <p style="font-size:12px;color:#6b7280;margin-top:20px;">
        Or copy and paste this link into your browser:<br>
        <span style="word-break:break-all;">{verify_url}</span>
      </p>
      <p style="font-size:12px;color:#9ca3af;margin-top:22px;">
        If you did not sign up for DirectIndex Pro, you can ignore this message.
      </p>
    </td></tr>
  </table>
</body></html>"""


def _build_message(to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


async def send_email(to: str, subject: str, text: str, html: str | None = None) -> SentEmail:
    """
    Dispatch an email. Returns a SentEmail record regardless of path (dev
    capture vs real SMTP) so callers can confirm delivery was attempted.
    """
    record = SentEmail(to=to, subject=subject, text=text, html=html)
    _SENT.append(record)

    if not settings.SMTP_HOST:
        logger.info(
            "[email-dev] to=%s subject=%s\n%s", to, subject, text,
        )
        return record

    msg = _build_message(to, subject, text, html)
    try:
        import aiosmtplib
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            start_tls=settings.SMTP_PORT != 465,
            use_tls=settings.SMTP_PORT == 465,
            username=settings.SMTP_USER or None,
            password=settings.SMTP_PASSWORD or None,
            timeout=15,
        )
    except ImportError:
        # Sync stdlib fallback — less preferred, but works without aiosmtplib
        import smtplib
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
    return record


async def send_verification_email(db: AsyncSession, user: User) -> str:
    """Generate a verification token and dispatch the email. Returns the token."""
    token = _make_token(user.id)
    verify_url = f"{settings.APP_BASE_URL}/api/auth/verify-email?token={token}"
    text = (
        f"Welcome to DirectIndex Pro.\n\n"
        f"Click the link below to verify your email. The link expires in 24 hours.\n\n"
        f"{verify_url}\n\n"
        f"If you did not sign up, you can ignore this message."
    )
    html = _render_verification_html(verify_url)
    await send_email(
        to=user.email,
        subject="Verify your email — DirectIndex Pro",
        text=text, html=html,
    )
    return token


async def verify_email_token(db: AsyncSession, token: str) -> User:
    """Raises jwt.InvalidTokenError / jwt.ExpiredSignatureError on bad tokens."""
    user_id = _decode_token(token)
    user = await db.get(User, user_id)
    if not user:
        raise jwt.InvalidTokenError("user not found")
    if not user.email_verified:
        user.email_verified = True
        await db.commit()
        await db.refresh(user)
    return user
