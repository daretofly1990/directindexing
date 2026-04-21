"""
Idempotency-Key handling for state-changing endpoints.

Clients send `Idempotency-Key: <uuid>` on POST requests that must not double-run
(Stripe pattern). On first receipt we execute and cache the response. On retry
we return the cached response without re-executing.

Scope: (key, user_id, endpoint). TTL: 24h (callers pruning is a future job).

Usage pattern inside a route:

    key = request.headers.get("Idempotency-Key")
    if key:
        cached = await get_cached_response(db, key, user.id, endpoint)
        if cached is not None:
            return cached
    result = await do_work()
    if key:
        await cache_response(db, key, user.id, endpoint, result)
    return result
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models.models import IdempotencyRecord

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(hours=24)


async def get_cached_response(
    db: AsyncSession, key: str, user_id: int, endpoint: str,
) -> dict | None:
    r = await db.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.key == key,
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.endpoint == endpoint,
        )
    )
    rec = r.scalar_one_or_none()
    if rec is None:
        return None
    if datetime.utcnow() - rec.created_at > CACHE_TTL:
        return None
    try:
        return json.loads(rec.response_body or "null")
    except json.JSONDecodeError:
        return None


async def cache_response(
    db: AsyncSession, key: str, user_id: int, endpoint: str, response: dict,
    status_code: int = 200,
) -> None:
    rec = IdempotencyRecord(
        key=key,
        user_id=user_id,
        endpoint=endpoint,
        status_code=status_code,
        response_body=json.dumps(response, default=str),
    )
    db.add(rec)
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent request with same key raced — the other one will have
        # committed; discard ours.
        await db.rollback()
        logger.info("Idempotency race on key=%s user=%s endpoint=%s", key, user_id, endpoint)
