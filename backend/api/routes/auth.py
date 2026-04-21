from fastapi import APIRouter, HTTPException, status, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...auth import create_access_token, get_current_user
from ...rate_limit import limiter, AUTH_TOKEN_LIMIT
from ...services.totp_service import generate_secret, otpauth_uri, verify_code
from ...services.user_service import authenticate

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token")
@limiter.limit(AUTH_TOKEN_LIMIT)
async def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    # Admin accounts with TOTP enabled must present a valid code via the
    # `client_secret` form field (reused from OAuth2PasswordRequestForm).
    # Non-admin users and admins who haven't enrolled skip this step.
    user = await authenticate(db, form.username, form.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if user.role == "admin" and user.totp_enabled:
        code = (form.client_secret or "").strip()
        if not code:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "TOTP code required")
        if not verify_code(user.totp_secret or "", code):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid TOTP code")
    token = create_access_token(user.id, user.role)
    return {"access_token": token, "token_type": "bearer"}


class TotpEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TotpVerifyRequest(BaseModel):
    code: str


@router.post("/mfa/enroll", response_model=TotpEnrollResponse)
async def mfa_enroll(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate (or rotate) the TOTP secret for an admin. Returns an otpauth URI
    the caller should render as a QR code, plus the base32 secret for manual
    entry. Does NOT enable MFA yet — that requires a round-trip through
    /mfa/verify with a real code so we know the app copied the secret.
    """
    if current_user.role != "admin":
        raise HTTPException(403, "MFA enrollment is for admin users only")
    secret = generate_secret()
    current_user.totp_secret = secret
    current_user.totp_enabled = False
    await db.commit()
    await db.refresh(current_user)
    uri = otpauth_uri(current_user, secret)
    return TotpEnrollResponse(secret=secret, otpauth_uri=uri)


@router.post("/mfa/verify")
async def mfa_verify(
    req: TotpVerifyRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm the authenticator app has the secret; enables MFA on success."""
    if current_user.role != "admin":
        raise HTTPException(403, "MFA is for admin users only")
    if not current_user.totp_secret:
        raise HTTPException(400, "Call /mfa/enroll first")
    if not verify_code(current_user.totp_secret, req.code):
        raise HTTPException(400, "Invalid code — try the next 30-second window")
    current_user.totp_enabled = True
    await db.commit()
    return {"enabled": True}


@router.post("/mfa/disable")
async def mfa_disable(
    req: TotpVerifyRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn MFA off. Requires a current code to prevent attacker-in-the-session disable."""
    if not current_user.totp_enabled:
        return {"enabled": False}
    if not verify_code(current_user.totp_secret or "", req.code):
        raise HTTPException(400, "Invalid code")
    current_user.totp_enabled = False
    current_user.totp_secret = None
    await db.commit()
    return {"enabled": False}


@router.get("/verify-email")
async def verify_email(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """GET link from verification email. Flips `User.email_verified=True`."""
    import jwt as _jwt
    from ...services.email_service import verify_email_token
    try:
        user = await verify_email_token(db, token)
    except _jwt.ExpiredSignatureError:
        raise HTTPException(400, "Verification link expired — request a new one.")
    except _jwt.InvalidTokenError:
        raise HTTPException(400, "Invalid verification link.")
    return {"email": user.email, "verified": True}


@router.post("/resend-verification")
async def resend_verification(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from ...services.email_service import send_verification_email
    if current_user.email_verified:
        return {"email": current_user.email, "already_verified": True}
    await send_verification_email(db, current_user)
    return {"email": current_user.email, "sent": True}


@router.get("/me")
async def me(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from ...models.models import Client, Acknowledgement
    from sqlalchemy import select
    from .acknowledgements import CURRENT_VERSIONS

    self_client = None
    if current_user.role == "individual":
        r = await db.execute(
            select(Client).where(
                Client.advisor_id == current_user.id,
                Client.is_self == True,  # noqa: E712
            )
        )
        c = r.scalar_one_or_none()
        if c:
            self_client = {"id": c.id, "tax_rate_short": c.tax_rate_short, "tax_rate_long": c.tax_rate_long}

    # Reuse the ack-service helper so /me and /required agree on what's
    # missing, including the annual re-accept freshness check.
    from .acknowledgements import user_has_accepted
    missing_acks = []
    for doc in CURRENT_VERSIONS.keys():
        if not await user_has_accepted(db, current_user.id, doc):
            missing_acks.append(doc)

    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role,
        "persona": current_user.role,   # alias for clarity
        "email_verified": bool(current_user.email_verified),
        "totp_enabled": bool(current_user.totp_enabled),
        "self_client": self_client,
        "missing_acknowledgements": missing_acks,
        "features": {
            "can_run_advisor": len(missing_acks) == 0,   # ADV must be accepted first
            "admin_console": current_user.role == "admin",
            "clients_tab": current_user.role in ("admin", "advisor"),
        },
    }
