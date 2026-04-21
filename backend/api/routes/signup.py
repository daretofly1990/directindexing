"""
Public signup routes for retail individuals.

POST /api/signup/individual — creates a new User(role=individual) + self-Client,
returns a JWT so the UI can continue straight into the onboarding wizard.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import create_access_token
from ...database import get_db
from ...rate_limit import limiter, SIGNUP_LIMIT
from ...services.email_service import send_verification_email
from ...services.user_service import create_individual_user

router = APIRouter(prefix="/signup", tags=["signup"])


class IndividualSignupRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    tax_rate_short: float = 0.37
    tax_rate_long: float = 0.20


@router.post("/individual")
@limiter.limit(SIGNUP_LIMIT)
async def signup_individual(
    request: Request,
    req: IndividualSignupRequest,
    db: AsyncSession = Depends(get_db),
):
    if len(req.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    try:
        user, client = await create_individual_user(
            db,
            email=req.email,
            password=req.password,
            full_name=req.full_name,
            tax_rate_short=req.tax_rate_short,
            tax_rate_long=req.tax_rate_long,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Email already registered")
    # Fire verification email (logs to stdout in dev when SMTP_HOST unset)
    try:
        await send_verification_email(db, user)
    except Exception:
        pass  # non-fatal — user can resend via /api/auth/resend-verification
    token = create_access_token(user.id, user.role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "role": user.role},
        "client": {"id": client.id, "is_self": True},
        "next_steps": ["verify_email", "accept_tos", "accept_adv", "upload_lots_or_construct"],
    }
