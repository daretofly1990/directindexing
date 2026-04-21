"""
JWT authentication — multi-user, role-aware.

Token payload: {"sub": "<user_id>", "role": "<role>", "exp": <timestamp>}

Roles:
  admin   — full access including user management and admin routes
  advisor — access to own clients' portfolios only
"""
from datetime import datetime, timedelta
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import get_db

_bearer = HTTPBearer()


@dataclass
class TokenData:
    user_id: int
    role: str


def create_access_token(user_id: int, role: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "role": role, "exp": expire},
        settings.JWT_SECRET,
        algorithm="HS256",
    )


def _decode(token: str) -> TokenData:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        return TokenData(user_id=int(payload["sub"]), role=payload.get("role", "advisor"))
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> TokenData:
    return _decode(credentials.credentials)


async def get_current_user(
    token_data: TokenData = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    from .models.models import User
    user = await db.get(User, token_data.user_id)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    return user


def require_admin(token_data: TokenData = Depends(verify_token)) -> TokenData:
    if token_data.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return token_data
