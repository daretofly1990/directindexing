"""Tests for JWT token creation, verification, and expiry."""
import time
import pytest
import jwt

from backend.auth import create_access_token, verify_token
from backend.config import settings


def _make_token(user_id: int = 1, role: str = "advisor", exp_minutes: int = 30) -> str:
    return create_access_token(user_id, role)


def test_create_and_decode_token():
    token = _make_token(user_id=42, role="admin")
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    assert payload["sub"] == "42"
    assert payload["role"] == "admin"


def test_verify_token_returns_token_data():
    from fastapi.security import HTTPAuthorizationCredentials
    token = _make_token(user_id=7, role="advisor")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    class _FakeRequest:
        pass

    token_data = verify_token(creds)
    assert token_data.user_id == 7
    assert token_data.role == "advisor"


def test_expired_token_raises():
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from datetime import datetime, timezone, timedelta

    payload = {
        "sub": "1",
        "role": "advisor",
        "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
    }
    expired = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired)

    with pytest.raises(HTTPException) as exc_info:
        verify_token(creds)
    assert exc_info.value.status_code == 401


def test_tampered_token_raises():
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    token = _make_token() + "tampered"
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as exc_info:
        verify_token(creds)
    assert exc_info.value.status_code == 401
