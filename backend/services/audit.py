"""
Audit helpers: write AuditEvent and RecommendationLog rows.

These tables are append-only by convention (no UPDATE / DELETE in any code path).
The Alembic migration grants only INSERT/SELECT to the app role in production
(manual step — see docs/compliance.md once written).
"""
import json
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.models import AuditEvent, RecommendationLog

logger = logging.getLogger(__name__)


async def log_audit(
    db: AsyncSession,
    event_type: str,
    user_id: int | None = None,
    portfolio_id: int | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> AuditEvent:
    ev = AuditEvent(
        user_id=user_id,
        event_type=event_type,
        portfolio_id=portfolio_id,
        object_type=object_type,
        object_id=object_id,
        details_json=json.dumps(details) if details is not None else None,
        ip_address=ip_address,
    )
    db.add(ev)
    await db.flush()
    return ev


async def log_recommendation(
    db: AsyncSession,
    user_id: int,
    portfolio_id: int,
    prompt: str,
    reasoning: str,
    tool_calls: list,
    draft_plan: dict | None,
    model_version: str | None,
    prompt_version: str | None,
    adv_version_acknowledged: str | None,
    demo_mode: bool,
) -> RecommendationLog:
    log = RecommendationLog(
        user_id=user_id,
        portfolio_id=portfolio_id,
        prompt=prompt,
        model_version=model_version,
        prompt_version=prompt_version,
        tool_calls_json=json.dumps(tool_calls, default=str)[:2_000_000],
        reasoning=reasoning[:1_000_000] if reasoning else None,
        draft_plan_json=json.dumps(draft_plan) if draft_plan else None,
        adv_version_acknowledged=adv_version_acknowledged,
        demo_mode=demo_mode,
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return log
