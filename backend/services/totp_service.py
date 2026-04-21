"""
TOTP-based MFA for admin accounts (RFC 6238, 30-second window, 6 digits).

Flow:
  1. Admin hits POST /api/auth/mfa/enroll → returns otpauth URI + secret
     (user scans QR in authenticator app)
  2. Admin hits POST /api/auth/mfa/verify {code} once to prove the app has
     the secret; that flips `User.totp_enabled=True`.
  3. On every future /api/auth/token call for this admin, the OAuth2 form
     must include `client_secret=<6-digit-code>` (OAuth2PasswordRequestForm
     exposes this field). Missing or wrong code → 401.

We use pyotp's TOTP with a 1-step lookback window so a code that was valid
when the user typed it remains valid when the server processes it.
"""
import pyotp

from ..config import settings
from ..models.models import User


def generate_secret() -> str:
    return pyotp.random_base32()


def otpauth_uri(user: User, secret: str) -> str:
    """Return an otpauth URI. Paste into a QR lib on the frontend or display as fallback text."""
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=user.email,
        issuer_name=settings.TOTP_ISSUER,
    )


def verify_code(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    # valid_window=1 tolerates 30s clock drift on either side
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
