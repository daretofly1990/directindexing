"""
Rate limiting via SlowAPI. Keyed on client IP.

Limits:
  - /api/auth/token    — 5/minute + 20/hour  (brute-force / credential stuffing)
  - /api/signup/individual — 3/minute + 20/hour  (disposable-email churn)

SlowAPI's in-memory backend is fine for a single-process dev setup; for
multi-process deployments, point it at Redis:

    from slowapi.util import get_remote_address
    limiter = Limiter(
        key_func=get_remote_address,
        storage_uri="redis://redis:6379",
    )

The Request object is required on any route that uses these limits, since
SlowAPI extracts the client IP from it.
"""
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from fastapi import Request
from fastapi.responses import JSONResponse

limiter = Limiter(key_func=get_remote_address, default_limits=[])


def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )


# Named limits — referenced from route decorators
AUTH_TOKEN_LIMIT = "5/minute;20/hour"
SIGNUP_LIMIT = "3/minute;20/hour"
