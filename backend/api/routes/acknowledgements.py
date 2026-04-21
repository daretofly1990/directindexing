"""
Acknowledgement routes: capture ToS / ADV Part 2A / Privacy Notice acceptance.

ADV Part 2A must be acknowledged before any personalized recommendation is
generated. The current app version of each document is served via GET so the
frontend can render and display it.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ...auth import get_current_user
from ...database import get_db
from ...models.models import Acknowledgement
from ...services.audit import log_audit

router = APIRouter(prefix="/acknowledgements", tags=["acknowledgements"])


# Current document versions (bump when the document changes)
CURRENT_VERSIONS = {
    "tos": "2026-04-19-v1",
    "adv_part_2a": "2026-04-19-v1",
    "privacy": "2026-04-19-v1",
}

VALID_DOCS = set(CURRENT_VERSIONS.keys())

# Annual-re-acceptance requirements per Reg S-P (privacy) and general best
# practice for ADV brochure refresh. ToS doesn't legally require annual
# re-acceptance — only re-accept when you bump the version string.
ANNUAL_REACCEPT_DOCS = {"privacy", "adv_part_2a"}
REACCEPT_MAX_AGE_DAYS = 365


class AckRequest(BaseModel):
    document_type: str   # tos | adv_part_2a | privacy
    version: str | None = None  # defaults to current


@router.get("/required")
async def required_ack(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Which ack docs this user has NOT yet accepted at their current version, OR
    has accepted but the acceptance is older than the annual re-accept window
    (Reg S-P). A row shows up in `missing` if either condition is true.

    Stale acceptances are reported with `reason: "annual_reaccept"` so the
    frontend can pick a different tone ("please re-confirm your privacy
    preferences" vs "please accept this document").
    """
    from datetime import datetime as _dt, timedelta as _td
    result = await db.execute(
        select(Acknowledgement).where(Acknowledgement.user_id == current_user.id)
    )
    rows = result.scalars().all()
    # Most recent ack per (doc_type, version)
    latest_by_key: dict[tuple[str, str], Acknowledgement] = {}
    for a in rows:
        k = (a.document_type, a.version)
        if k not in latest_by_key or a.accepted_at > latest_by_key[k].accepted_at:
            latest_by_key[k] = a

    now = _dt.utcnow()
    missing = []
    for doc, ver in CURRENT_VERSIONS.items():
        ack = latest_by_key.get((doc, ver))
        if ack is None:
            missing.append({"document_type": doc, "version": ver, "reason": "never_accepted"})
            continue
        # Annual re-accept window
        if doc in ANNUAL_REACCEPT_DOCS:
            age = now - ack.accepted_at
            if age > _td(days=REACCEPT_MAX_AGE_DAYS):
                missing.append({
                    "document_type": doc,
                    "version": ver,
                    "reason": "annual_reaccept",
                    "last_accepted_at": ack.accepted_at.isoformat(),
                    "days_since_accepted": age.days,
                })
    return {
        "current_versions": CURRENT_VERSIONS,
        "missing": missing,
        "annual_docs": sorted(ANNUAL_REACCEPT_DOCS),
        "max_age_days": REACCEPT_MAX_AGE_DAYS,
    }


@router.post("")
async def accept_ack(
    req: AckRequest,
    request: Request,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.document_type not in VALID_DOCS:
        raise HTTPException(422, f"Unknown document_type. Must be one of {sorted(VALID_DOCS)}")
    version = req.version or CURRENT_VERSIONS[req.document_type]
    ack = Acknowledgement(
        user_id=current_user.id,
        document_type=req.document_type,
        version=version,
        ip_address=request.client.host if request.client else None,
    )
    db.add(ack)
    await db.flush()
    await log_audit(
        db,
        event_type="ACKNOWLEDGEMENT_ACCEPTED",
        user_id=current_user.id,
        object_type="acknowledgement",
        object_id=ack.id,
        details={"document_type": req.document_type, "version": version},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(ack)
    return {
        "id": ack.id,
        "document_type": ack.document_type,
        "version": ack.version,
        "accepted_at": ack.accepted_at.isoformat(),
    }


async def user_has_accepted(db: AsyncSession, user_id: int, document_type: str) -> bool:
    """
    Has this user accepted the CURRENT version of `document_type`, AND
    (for annual-reaccept docs) is the acceptance still within window?
    """
    from datetime import datetime as _dt, timedelta as _td
    version = CURRENT_VERSIONS.get(document_type)
    if not version:
        return False
    result = await db.execute(
        select(Acknowledgement).where(
            Acknowledgement.user_id == user_id,
            Acknowledgement.document_type == document_type,
            Acknowledgement.version == version,
        ).order_by(Acknowledgement.accepted_at.desc())
    )
    ack = result.scalar_one_or_none()
    if ack is None:
        return False
    if document_type in ANNUAL_REACCEPT_DOCS:
        if (_dt.utcnow() - ack.accepted_at) > _td(days=REACCEPT_MAX_AGE_DAYS):
            return False
    return True
