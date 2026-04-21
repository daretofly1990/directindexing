"""
M7 ops smoke tests: TOTP, secrets fallback, Sentry scrubbing, backup skip-path.
These tests don't touch AWS, Sentry, or a real DB — they prove the plumbing.
"""
from unittest.mock import patch

import pyotp
import pytest

from backend.observability import _before_send, _scrub_mapping
from backend.services import secrets as secrets_mod
from backend.services import totp_service


# ---------------- TOTP ----------------

def test_totp_generate_and_verify_roundtrip():
    secret = totp_service.generate_secret()
    code = pyotp.TOTP(secret).now()
    assert totp_service.verify_code(secret, code) is True


def test_totp_rejects_wrong_code():
    secret = totp_service.generate_secret()
    assert totp_service.verify_code(secret, "000000") is False


def test_totp_rejects_empty_inputs():
    assert totp_service.verify_code("", "123456") is False
    assert totp_service.verify_code("JBSWY3DPEHPK3PXP", "") is False


def test_totp_uri_format():
    class FakeUser:
        email = "admin@example.com"
    uri = totp_service.otpauth_uri(FakeUser(), "JBSWY3DPEHPK3PXP")
    assert uri.startswith("otpauth://totp/")
    # pyotp URL-encodes the @ as %40
    assert "admin%40example.com" in uri or "admin@example.com" in uri


# ---------------- Secrets fallback ----------------

def test_secrets_fallback_to_env(monkeypatch):
    secrets_mod.clear_cache()
    monkeypatch.setenv("MY_TEST_SECRET", "from-env")
    # Force AWS lookup to fail by disabling prefix
    monkeypatch.setattr(secrets_mod, "_fetch_from_aws", lambda k: None)
    val = secrets_mod.load_secret("MY_TEST_SECRET", default="nope")
    assert val == "from-env"


def test_secrets_fallback_to_default(monkeypatch):
    secrets_mod.clear_cache()
    monkeypatch.delenv("UNSET_ANYWHERE_SECRET", raising=False)
    monkeypatch.setattr(secrets_mod, "_fetch_from_aws", lambda k: None)
    val = secrets_mod.load_secret("UNSET_ANYWHERE_SECRET", default="fallback-value")
    assert val == "fallback-value"


def test_secrets_cache_returns_same_value(monkeypatch):
    secrets_mod.clear_cache()
    monkeypatch.setenv("CACHED_SECRET", "v1")
    monkeypatch.setattr(secrets_mod, "_fetch_from_aws", lambda k: None)
    assert secrets_mod.load_secret("CACHED_SECRET") == "v1"
    # Changing the env var should NOT affect the cached read
    monkeypatch.setenv("CACHED_SECRET", "v2")
    assert secrets_mod.load_secret("CACHED_SECRET") == "v1"


def test_secrets_aws_takes_precedence_over_env(monkeypatch):
    secrets_mod.clear_cache()
    monkeypatch.setenv("PRECEDENCE_SECRET", "from-env")
    monkeypatch.setattr(secrets_mod, "_fetch_from_aws", lambda k: "from-aws")
    assert secrets_mod.load_secret("PRECEDENCE_SECRET") == "from-aws"


# ---------------- Sentry scrubbing ----------------

def test_sentry_scrubs_auth_header():
    event = {"request": {"headers": {"authorization": "Bearer xyz", "user-agent": "curl"}}}
    _before_send(event, None)
    assert event["request"]["headers"]["authorization"] == "[redacted]"
    assert event["request"]["headers"]["user-agent"] == "curl"


def test_sentry_scrubs_password_like_keys():
    scrubbed = _scrub_mapping({
        "email": "a@b.com", "password": "hunter2",
        "api_key": "sk_live_abc", "totp_secret": "JBSW",
        "unrelated": "ok",
    })
    assert scrubbed["email"] == "a@b.com"
    assert scrubbed["password"] == "[redacted]"
    assert scrubbed["api_key"] == "[redacted]"
    assert scrubbed["totp_secret"] == "[redacted]"
    assert scrubbed["unrelated"] == "ok"


def test_sentry_drops_cookies():
    event = {"request": {"cookies": {"session": "xyz"}}}
    _before_send(event, None)
    assert "cookies" not in event["request"]


# ---------------- Backup skip path ----------------

@pytest.mark.asyncio
async def test_backup_skipped_when_bucket_unset(monkeypatch):
    from backend.services import backup_service as b
    monkeypatch.setattr(b.settings, "S3_BACKUP_BUCKET", "")
    r = await b.run_backup()
    assert r["status"] == "skipped"
    assert "S3_BACKUP_BUCKET" in r["reason"]


@pytest.mark.asyncio
async def test_backup_skipped_for_sqlite(monkeypatch):
    from backend.services import backup_service as b
    monkeypatch.setattr(b.settings, "S3_BACKUP_BUCKET", "fake-bucket")
    monkeypatch.setattr(b.settings, "DATABASE_URL", "sqlite+aiosqlite:///./dev.db")
    r = await b.run_backup()
    assert r["status"] == "skipped"
    assert "Postgres" in r["reason"]
