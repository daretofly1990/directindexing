from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...auth import require_admin, get_current_user
from ...models.models import AuditEvent
from ...services.audit import log_audit
from ...services.notifications import OPT_IN_EVENT, OPT_OUT_EVENT
from ...services.user_service import create_user, get_all_users

router = APIRouter(prefix="/users", tags=["users"])


class NotificationPrefs(BaseModel):
    harvest_opportunities: bool


@router.get("/me/notifications")
async def get_notification_prefs(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Most-recent opt-in/opt-out wins. Default = opted in."""
    r = await db.execute(
        select(AuditEvent).where(
            AuditEvent.user_id == current_user.id,
            AuditEvent.event_type.in_([OPT_IN_EVENT, OPT_OUT_EVENT]),
        ).order_by(AuditEvent.created_at.desc()).limit(1)
    )
    ev = r.scalar_one_or_none()
    opted_in = (ev is None) or (ev.event_type == OPT_IN_EVENT)
    return {"harvest_opportunities": opted_in}


@router.post("/me/notifications")
async def set_notification_prefs(
    req: NotificationPrefs,
    request: Request,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    event = OPT_IN_EVENT if req.harvest_opportunities else OPT_OUT_EVENT
    await log_audit(
        db, event_type=event,
        user_id=current_user.id,
        details={"harvest_opportunities": req.harvest_opportunities},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"harvest_opportunities": req.harvest_opportunities}


class CreateUserRequest(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    role: str = "advisor"


@router.get("")
async def list_users(_=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    users = await get_all_users(db)
    return [
        {"id": u.id, "email": u.email, "full_name": u.full_name,
         "role": u.role, "is_active": u.is_active, "created_at": u.created_at.isoformat()}
        for u in users
    ]


@router.post("")
async def create(
    req: CreateUserRequest,
    request: Request,
    current_user=Depends(get_current_user),
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if req.role not in ("admin", "advisor"):
        raise HTTPException(400, "role must be 'admin' or 'advisor'")
    try:
        user = await create_user(db, req.email, req.password, req.role, req.full_name)
    except Exception:
        raise HTTPException(409, "Email already exists")
    await log_audit(
        db, event_type="USER_CREATED",
        user_id=current_user.id,
        object_type="user", object_id=user.id,
        details={"email": user.email, "role": user.role},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"id": user.id, "email": user.email, "role": user.role}
