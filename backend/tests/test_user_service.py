"""Tests for user creation, authentication, and admin bootstrap."""
import pytest

from backend.services.user_service import create_user, authenticate, ensure_admin, hash_password, verify_password


@pytest.mark.asyncio
async def test_create_and_authenticate(db):
    user = await create_user(db, "advisor@example.com", "secret123", role="advisor")
    assert user.id is not None
    assert user.role == "advisor"

    authenticated = await authenticate(db, "advisor@example.com", "secret123")
    assert authenticated is not None
    assert authenticated.id == user.id


@pytest.mark.asyncio
async def test_wrong_password_fails(db):
    await create_user(db, "user@test.com", "correct_password")
    result = await authenticate(db, "user@test.com", "wrong_password")
    assert result is None


@pytest.mark.asyncio
async def test_unknown_email_fails(db):
    result = await authenticate(db, "nobody@test.com", "any")
    assert result is None


@pytest.mark.asyncio
async def test_ensure_admin_idempotent(db):
    """ensure_admin should not raise if called twice."""
    await ensure_admin(db, "admin@di.com", "admin_pass")
    await ensure_admin(db, "admin@di.com", "admin_pass")


@pytest.mark.asyncio
async def test_ensure_admin_creates_admin_role(db):
    await ensure_admin(db, "admin@di.com", "admin_pass")
    admin = await authenticate(db, "admin@di.com", "admin_pass")
    assert admin is not None
    assert admin.role == "admin"


def test_hash_and_verify():
    hashed = hash_password("my_password")
    assert verify_password("my_password", hashed)
    assert not verify_password("wrong", hashed)
