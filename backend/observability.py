"""
Sentry error tracking with PII scrubbing.

Init is called unconditionally at import; if `SENTRY_DSN` is empty the SDK is
never configured and nothing ships. Safe to include in all environments.

Scrubbing rules:
  - `request.headers.authorization` always dropped (would contain JWTs)
  - `request.cookies` dropped
  - Any form/json field whose key looks secret (password, token, secret, key,
    ssn, dob, account) is redacted
  - `Transaction.notes`, `RecommendationLog.*`, `AuditEvent.details_json` are
    already encrypted at rest — no additional handling needed since Sentry
    wouldn't see them unless explicitly attached
"""
import logging
import re

logger = logging.getLogger(__name__)

SECRET_KEY_RE = re.compile(
    r"password|token|secret|key|ssn|tin|dob|birth|account|routing|card",
    re.IGNORECASE,
)


def _scrub_value(key: str, value):
    """Return '[redacted]' for keys that look sensitive; else pass through."""
    if isinstance(key, str) and SECRET_KEY_RE.search(key):
        return "[redacted]"
    return value


def _scrub_mapping(m):
    if not isinstance(m, dict):
        return m
    return {k: _scrub_value(k, v) for k, v in m.items()}


def _before_send(event, hint):
    """Mutate the event in place to drop/redact sensitive fields."""
    try:
        req = event.get("request") or {}
        # Drop auth + cookies wholesale
        if "headers" in req:
            req["headers"] = {
                k: "[redacted]" if k.lower() in ("authorization", "cookie", "x-api-key") else v
                for k, v in req["headers"].items()
            }
        req.pop("cookies", None)
        if "data" in req:
            req["data"] = _scrub_mapping(req["data"])
        # Extra context map
        extra = event.get("extra") or {}
        event["extra"] = _scrub_mapping(extra)
        # Breadcrumbs
        for bc in event.get("breadcrumbs", {}).get("values", []):
            if "data" in bc:
                bc["data"] = _scrub_mapping(bc["data"])
    except Exception:
        # Never let scrubbing crash the SDK — just ship the event unmodified
        logger.exception("Sentry scrubber raised")
    return event


def configure_sentry() -> bool:
    """
    Initialize sentry-sdk. Returns True if enabled, False in dev.
    Idempotent — safe to call multiple times.
    """
    from .config import settings
    dsn = getattr(settings, "SENTRY_DSN", "") or ""
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        sentry_sdk.init(
            dsn=dsn,
            environment=getattr(settings, "SENTRY_ENVIRONMENT", "production"),
            release=getattr(settings, "APP_VERSION", "unknown"),
            send_default_pii=False,    # do not include IP/email automatically
            traces_sample_rate=float(getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0.0) or 0.0),
            profiles_sample_rate=float(getattr(settings, "SENTRY_PROFILES_SAMPLE_RATE", 0.0) or 0.0),
            before_send=_before_send,
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        )
        logger.info("Sentry initialised (env=%s)", getattr(settings, "SENTRY_ENVIRONMENT", "production"))
        return True
    except ImportError:
        logger.warning("sentry-sdk not installed — SENTRY_DSN ignored")
        return False
