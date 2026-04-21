"""
Secret loader with graceful fallback: AWS Secrets Manager → env vars → default.

Use for secrets that shouldn't live in `.env` in production (JWT signing keys,
Stripe webhook secrets, Fernet keys, SMTP passwords). Keep `.env` for local
dev; in prod, set AWS creds + `AWS_SECRETS_PREFIX` and the app pulls on boot.

Naming convention:
  AWS_SECRETS_PREFIX = "direct-indexing/"
  → env var JWT_SECRET maps to AWS secret "direct-indexing/jwt_secret"

Cached per-process for the life of the container; restart to rotate.
"""
import logging
import os

logger = logging.getLogger(__name__)

_CACHE: dict[str, str] = {}


def _aws_key_for(env_name: str, prefix: str) -> str:
    """env var JWT_SECRET -> 'direct-indexing/jwt_secret'"""
    return f"{prefix.rstrip('/')}/{env_name.lower()}"


def _fetch_from_aws(key: str) -> str | None:
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return None
    try:
        from ..config import settings
        client = boto3.session.Session().client(
            "secretsmanager", region_name=settings.AWS_REGION,
        )
        resp = client.get_secret_value(SecretId=key)
        return resp.get("SecretString")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("ResourceNotFoundException", "AccessDeniedException"):
            logger.warning("Secrets Manager error for %s: %s", key, exc)
        return None
    except Exception as exc:
        logger.warning("Secrets Manager unreachable for %s: %s", key, exc)
        return None


def load_secret(env_name: str, default: str = "") -> str:
    """
    Resolve a secret in this precedence:
      1. In-process cache (so boto is called at most once per process)
      2. AWS Secrets Manager at <prefix>/<env_name.lower()> if AWS is reachable
      3. Plain env var `env_name`
      4. `default`
    """
    if env_name in _CACHE:
        return _CACHE[env_name]

    from ..config import settings
    prefix = settings.AWS_SECRETS_PREFIX or ""
    value = None
    if prefix:
        key = _aws_key_for(env_name, prefix)
        value = _fetch_from_aws(key)
    if value is None:
        value = os.getenv(env_name, default)
    _CACHE[env_name] = value or ""
    return _CACHE[env_name]


def clear_cache() -> None:
    """Testing helper — force a fresh lookup."""
    _CACHE.clear()
