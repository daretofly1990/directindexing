"""
Global trading kill switch.

When set, every trade-execution path (spec-ID sell, harvest, rebalance execute,
trade-plan approve/execute) returns 503. Toggled via admin endpoint and
persisted in the `system_flags` table, so it survives process restarts.

A 30-second in-memory cache avoids round-tripping the DB on every trade.
"""
import time

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import SystemFlag

FLAG_KEY = "trading_halted"
_CACHE_TTL = 30.0  # seconds

_cache: dict = {"ts": 0.0, "halted": False, "reason": None}


async def _load(db: AsyncSession) -> tuple[bool, str | None]:
    r = await db.execute(select(SystemFlag).where(SystemFlag.key == FLAG_KEY))
    row = r.scalar_one_or_none()
    if row is None:
        return False, None
    return row.value.lower() == "true", row.reason


async def is_halted(db: AsyncSession) -> tuple[bool, str | None]:
    now = time.monotonic()
    if now - _cache["ts"] < _CACHE_TTL:
        return _cache["halted"], _cache["reason"]
    halted, reason = await _load(db)
    _cache.update({"ts": now, "halted": halted, "reason": reason})
    return halted, reason


async def set_halted(
    db: AsyncSession, halted: bool, reason: str | None, user_id: int | None,
) -> dict:
    r = await db.execute(select(SystemFlag).where(SystemFlag.key == FLAG_KEY))
    row = r.scalar_one_or_none()
    if row is None:
        row = SystemFlag(
            key=FLAG_KEY, value="true" if halted else "false",
            reason=reason, updated_by_user_id=user_id,
        )
        db.add(row)
    else:
        row.value = "true" if halted else "false"
        row.reason = reason
        row.updated_by_user_id = user_id
        from datetime import datetime as _dt
        row.updated_at = _dt.utcnow()
    await db.commit()
    _cache["ts"] = 0.0   # bust cache
    return {"halted": halted, "reason": reason}


async def assert_trading_enabled(db: AsyncSession) -> None:
    """Raise 503 if the kill switch is engaged. Call this at the top of every trade route."""
    halted, reason = await is_halted(db)
    if halted:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Trading is currently halted. Reason: {reason or 'no reason provided'}",
        )
