"""
Email delivery tests — mocks aiosmtplib so the real path runs without
network / credentials, and verifies the dev fallback captures messages.
"""
from unittest.mock import AsyncMock, patch

import pytest

from backend.models.models import Portfolio, User
from backend.services import email_service
from backend.services.email_service import (
    send_email, send_verification_email, verify_email_token,
    captured_emails, clear_captured_emails,
)


@pytest.fixture(autouse=True)
def _reset_captures():
    clear_captured_emails()
    yield
    clear_captured_emails()


@pytest.mark.asyncio
async def test_dev_mode_captures_and_does_not_call_aiosmtplib(monkeypatch):
    """SMTP_HOST empty → log + capture, no aiosmtplib.send call."""
    monkeypatch.setattr(email_service.settings, "SMTP_HOST", "")
    mock_send = AsyncMock()
    with patch("aiosmtplib.send", mock_send):
        rec = await send_email("a@b.com", "subj", "body")
    assert rec.to == "a@b.com"
    assert rec.subject == "subj"
    assert mock_send.call_count == 0
    assert len(captured_emails()) == 1


@pytest.mark.asyncio
async def test_prod_mode_calls_aiosmtplib(monkeypatch):
    """SMTP_HOST set → aiosmtplib.send is invoked with the right host/port."""
    monkeypatch.setattr(email_service.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(email_service.settings, "SMTP_PORT", 587)
    monkeypatch.setattr(email_service.settings, "SMTP_USER", "user")
    monkeypatch.setattr(email_service.settings, "SMTP_PASSWORD", "pass")
    mock_send = AsyncMock()
    with patch("aiosmtplib.send", mock_send):
        await send_email("a@b.com", "subj", "body", html="<p>body</p>")
    assert mock_send.call_count == 1
    # kwargs include host, port, credentials
    _, kwargs = mock_send.call_args
    assert kwargs["hostname"] == "smtp.example.com"
    assert kwargs["port"] == 587
    assert kwargs["username"] == "user"
    assert kwargs["password"] == "pass"
    assert kwargs["start_tls"] is True
    assert kwargs["use_tls"] is False


@pytest.mark.asyncio
async def test_port_465_uses_implicit_tls(monkeypatch):
    """Port 465 → SMTPS (use_tls), not STARTTLS."""
    monkeypatch.setattr(email_service.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(email_service.settings, "SMTP_PORT", 465)
    mock_send = AsyncMock()
    with patch("aiosmtplib.send", mock_send):
        await send_email("a@b.com", "subj", "body")
    _, kwargs = mock_send.call_args
    assert kwargs["use_tls"] is True
    assert kwargs["start_tls"] is False


@pytest.mark.asyncio
async def test_verification_email_roundtrip(db, monkeypatch):
    """Full signup → verify flow with captured email in dev mode."""
    monkeypatch.setattr(email_service.settings, "SMTP_HOST", "")
    # Minimal User row
    user = User(email="r@t.com", hashed_password="x", role="individual")
    db.add(user); await db.commit(); await db.refresh(user)

    token = await send_verification_email(db, user)
    assert token is not None
    msgs = captured_emails()
    assert len(msgs) == 1
    assert msgs[0].to == "r@t.com"
    assert "verify" in msgs[0].subject.lower()
    assert msgs[0].html is not None and "Verify email" in msgs[0].html

    # Token round-trips and flips email_verified
    returned = await verify_email_token(db, token)
    assert returned.id == user.id
    assert returned.email_verified is True


@pytest.mark.asyncio
async def test_html_and_text_both_present_in_message(monkeypatch):
    """Verification email sets both text and HTML alternatives."""
    monkeypatch.setattr(email_service.settings, "SMTP_HOST", "smtp.example.com")
    captured_msgs = []

    async def fake_send(msg, **kwargs):
        captured_msgs.append(msg)
    with patch("aiosmtplib.send", side_effect=fake_send):
        await send_email("a@b.com", "subj", "plain text", html="<p>rich</p>")
    assert len(captured_msgs) == 1
    msg = captured_msgs[0]
    # multipart/alternative with both parts
    parts = list(msg.walk())
    content_types = [p.get_content_type() for p in parts]
    assert "text/plain" in content_types
    assert "text/html" in content_types
