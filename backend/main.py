import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .database import init_db, AsyncSessionLocal
from .auth import verify_token
from .config import settings
from .logging_config import configure_logging
from .observability import configure_sentry
from .rate_limit import limiter, rate_limit_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

configure_logging()
configure_sentry()
from .api.routes import (
    portfolios, tax_loss, esg, rebalancing,
    market, backtest, auth as auth_routes,
)
from .api.routes import admin as admin_routes
from .api.routes import users as user_routes
from .api.routes import clients as client_routes
from .api.routes import corporate_actions as corp_action_routes
from .api.routes import import_lots as import_lots_routes
from .api.routes import agent as agent_routes
from .api.routes import positions as positions_routes
from .api.routes import signup as signup_routes
from .api.routes import acknowledgements as ack_routes
from .api.routes import households as household_routes
from .api.routes import trade_plans as trade_plan_routes
from .api.routes import compliance as compliance_routes
from .api.routes import billing as billing_routes
from .api.routes import cpa_invites as cpa_invite_routes

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

_REFRESH_THRESHOLD_HOURS = 24


# ------------------------------------------------------------------ #
# Background tasks                                                     #
# ------------------------------------------------------------------ #

async def _maybe_refresh_index(index_name: str) -> None:
    from .services.constituent_store import last_refreshed, refresh_index
    async with AsyncSessionLocal() as db:
        ts = await last_refreshed(db, index_name)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if ts is None or (now - ts) > timedelta(hours=_REFRESH_THRESHOLD_HOURS):
            logger.info("Refreshing constituents: %s", index_name)
            try:
                rows = await refresh_index(db, index_name)
                logger.info("Refresh done %s: %d rows", index_name, len(rows))
            except Exception as exc:
                logger.error("Refresh failed %s: %s", index_name, exc)


async def _scheduled_constituent_refresh() -> None:
    for idx in ("sp500", "nasdaq100", "russell1000"):
        await _maybe_refresh_index(idx)


async def _scheduled_dividend_sweep() -> None:
    from .services.dividend_service import process_dividends_all_portfolios
    async with AsyncSessionLocal() as db:
        try:
            result = await process_dividends_all_portfolios(db, lookback_days=7)
            logger.info(
                "Dividend sweep: %d portfolios, %d dividends applied, $%.2f credited.",
                result["portfolios_processed"],
                result["total_dividends_applied"],
                result["total_cash_credited"],
            )
        except Exception as exc:
            logger.error("Dividend sweep failed: %s", exc)


async def _scheduled_backup() -> None:
    from .services.backup_service import run_backup
    try:
        r = await run_backup()
        logger.info("Nightly backup: status=%s", r.get("status"))
    except Exception as exc:
        logger.error("Nightly backup failed: %s", exc)


async def _scheduled_harvest_notify() -> None:
    from .services.notifications import scan_and_notify_all
    async with AsyncSessionLocal() as db:
        try:
            r = await scan_and_notify_all(db)
            logger.info(
                "Harvest-opportunity notify: sent=%d, skipped=%d, errors=%d",
                r["sent"], r["skipped"], r["errors"],
            )
        except Exception as exc:
            logger.error("Harvest notify failed: %s", exc)


async def _scheduled_retention_sweep() -> None:
    from .services.retention import retention_sweep
    async with AsyncSessionLocal() as db:
        try:
            r = await retention_sweep(db)
            logger.info(
                "Retention sweep: archived=%d, purged=%d",
                r["total_archived"], r["total_purged"],
            )
        except Exception as exc:
            logger.error("Retention sweep failed: %s", exc)


async def _scheduled_dr_drill() -> None:
    """
    Quarterly DR drill — runs the scratch-restore workflow and audits the
    result. No-op in environments where S3 backups or DR_DRILL_TARGET_URL
    aren't configured (dev, review apps, staging without a scratch DB).
    """
    from .services.dr_drill import run_dr_drill
    async with AsyncSessionLocal() as db:
        try:
            r = await run_dr_drill(db=db)
            logger.info("DR drill: status=%s elapsed=%ss",
                        r.get("status"), r.get("elapsed_seconds"))
        except Exception as exc:
            logger.error("DR drill failed: %s", exc)



# ------------------------------------------------------------------ #
# Bootstrap                                                            #
# ------------------------------------------------------------------ #

async def _ensure_admin() -> None:
    from .services.user_service import ensure_admin
    async with AsyncSessionLocal() as db:
        await ensure_admin(db, settings.ADMIN_EMAIL, settings.ADMIN_PASSWORD)


# ------------------------------------------------------------------ #
# App lifespan                                                         #
# ------------------------------------------------------------------ #

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _ensure_admin()

    # Kick off startup constituent refreshes
    for idx in ("sp500", "nasdaq100", "russell1000"):
        asyncio.create_task(_maybe_refresh_index(idx))

    # Scheduled jobs
    scheduler.add_job(_scheduled_constituent_refresh, "cron", hour=6, minute=0, id="constituent_refresh")
    scheduler.add_job(_scheduled_dividend_sweep, "cron", hour=6, minute=30, id="dividend_sweep")
    scheduler.add_job(_scheduled_retention_sweep, "cron", hour=3, minute=0, id="retention_sweep")
    scheduler.add_job(_scheduled_backup, "cron", hour=2, minute=0, id="nightly_backup")
    # 21:00 UTC = 5pm ET, after US market close — catches losses from today
    scheduler.add_job(_scheduled_harvest_notify, "cron", hour=21, minute=0, id="harvest_notify")
    # Quarterly DR drill — 1st of Jan/Apr/Jul/Oct at 04:00 UTC (pre-business-hours globally)
    scheduler.add_job(
        _scheduled_dr_drill, "cron",
        month="1,4,7,10", day=1, hour=4, minute=0,
        id="dr_drill",
    )
    scheduler.start()
    logger.info("Scheduler started with %d jobs.", len(scheduler.get_jobs()))

    yield

    scheduler.shutdown(wait=False)


# ------------------------------------------------------------------ #
# App setup                                                            #
# ------------------------------------------------------------------ #

app = FastAPI(title="Direct Indexing Platform", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)

_cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
if "*" in _cors_origins:
    logger.warning("CORS wildcard '*' in allowed origins — do not ship this to production.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
)


@app.middleware("http")
async def security_headers(request, call_next):
    """CSP + nosniff + no-referrer-leak — HSTS is set at the Caddy layer."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp

# Public routes (no JWT required)
app.include_router(auth_routes.router, prefix="/api")
app.include_router(signup_routes.router, prefix="/api")
# Billing: checkout/portal/status use get_current_user inline; webhook uses
# Stripe signature verification. Neither is compatible with the global JWT
# dependency, so the router is mounted as public.
app.include_router(billing_routes.router, prefix="/api")
# CPA magic-link viewer is intentionally public — the signed token IS the auth.
app.include_router(cpa_invite_routes.public_router, prefix="/api")

# Protected routes
_protected = [Depends(verify_token)]
for router in [
    portfolios.router,
    tax_loss.router,
    esg.router,
    rebalancing.router,
    market.router,
    backtest.router,
    admin_routes.router,
    user_routes.router,
    client_routes.router,
    corp_action_routes.router,
    import_lots_routes.router,
    agent_routes.router,
    positions_routes.router,
    ack_routes.router,
    household_routes.router,
    trade_plan_routes.router,
    compliance_routes.router,
    cpa_invite_routes.router,
]:
    app.include_router(router, prefix="/api", dependencies=_protected)

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(frontend_dir, "index.html"))


@app.get("/health")
async def health():
    """
    Liveness check. For readiness with dep probes (DB + Finnhub + Anthropic)
    see /health/deep. Kubernetes-style: this one stays cheap.
    """
    return {"status": "ok"}


async def _deep_health() -> dict:
    """Probe each external dependency; non-blocking on failures."""
    deps: dict[str, dict] = {}
    # Database
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(__import__("sqlalchemy").text("SELECT 1"))
        deps["database"] = {"status": "ok"}
    except Exception as exc:
        deps["database"] = {"status": "error", "error": str(exc)[:200]}
    # Finnhub
    try:
        from .services.finnhub_client import finnhub_client
        q = await finnhub_client.get_quote("SPY")
        deps["finnhub"] = {"status": "ok" if q.get("current_price") else "degraded"}
    except Exception as exc:
        deps["finnhub"] = {"status": "error", "error": str(exc)[:200]}
    # Anthropic — live probe if key is set (1-token ping), else unconfigured
    if settings.ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
            await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            deps["anthropic"] = {"status": "ok"}
        except Exception as exc:
            deps["anthropic"] = {"status": "error", "error": str(exc)[:200]}
    else:
        deps["anthropic"] = {"status": "unconfigured"}
    # PII encryption key
    deps["encryption"] = {
        "status": "ok" if settings.FIELD_ENCRYPTION_KEYS else "unconfigured",
    }
    overall = "ok" if all(d["status"] in ("ok", "unconfigured") for d in deps.values()) else "degraded"
    return {"status": overall, "dependencies": deps}


@app.get("/health/deep")
async def health_deep():
    return await _deep_health()


@app.get("/api/healthz")
async def api_healthz():
    """
    Deep health under /api — for load balancers / uptime probes that only route
    through the /api prefix. Exercises DB + Finnhub + Anthropic + encryption config.
    """
    return await _deep_health()
