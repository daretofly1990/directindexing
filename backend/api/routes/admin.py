"""
Admin API routes for index constituent management.

# TODO: auth — all endpoints should require an admin API key or JWT before going to production.
"""

import time
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import require_admin, get_current_user
from ...database import get_db
from ...services.constituent_store import (
    refresh_index,
    get_constituents,
    last_refreshed,
    VALID_INDEXES,
)
from ...services.dividend_service import (
    process_dividends_all_portfolios,
    process_dividends_for_portfolio,
)
from ...services.kill_switch import is_halted, set_halted
from ...services.audit import log_audit
from ...services.retention import retention_sweep
from ...services.backup_service import run_backup, list_backups
from ...services.notifications import scan_and_notify_all
from ...services.metrics_service import collect as collect_metrics
from ...services.dr_drill import run_dr_drill, drill_history_from_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

IndexParam = Literal["sp500", "nasdaq100", "russell1000", "all"]


@router.post("/constituents/refresh")
async def trigger_refresh(
    index: IndexParam = Query("all", description="Which index to refresh"),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger a live refresh of constituent data from upstream sources.

    Returns per-index stats: row count, as_of timestamp, duration_ms.
    """
    # TODO: auth — verify admin credentials before allowing refresh.
    targets = list(VALID_INDEXES) if index == "all" else [index]
    results = []

    for idx in targets:
        t0 = time.monotonic()
        try:
            rows = await refresh_index(db, idx)
            duration_ms = round((time.monotonic() - t0) * 1000)
            as_of = rows[0]["as_of"] if rows else None
            results.append({
                "index": idx,
                "count": len(rows),
                "as_of": as_of,
                "duration_ms": duration_ms,
                "status": "ok",
            })
        except Exception as exc:
            duration_ms = round((time.monotonic() - t0) * 1000)
            logger.exception("Refresh failed for %s: %s", idx, exc)
            results.append({
                "index": idx,
                "count": 0,
                "as_of": None,
                "duration_ms": duration_ms,
                "status": "error",
                "error": str(exc),
            })

    return {"refreshed": results}


@router.post("/dividends/process")
async def process_dividends(
    portfolio_id: int | None = Query(None, description="Process one portfolio only; omit for all"),
    lookback_days: int = Query(7, ge=1, le=365),
    current_user=Depends(get_current_user),
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually trigger a dividend sweep. Runs for a single portfolio (if portfolio_id
    is given) or all portfolios. Same logic as the nightly scheduled job.
    """
    if portfolio_id is not None:
        try:
            result = await process_dividends_for_portfolio(db, portfolio_id, lookback_days=lookback_days)
        except ValueError as e:
            raise HTTPException(404, str(e))
    else:
        result = await process_dividends_all_portfolios(db, lookback_days=lookback_days)
    await log_audit(
        db, event_type="DIVIDEND_SWEEP_RUN",
        user_id=current_user.id, portfolio_id=portfolio_id,
        details={
            "lookback_days": lookback_days,
            "dividends_applied": result.get("dividends_applied") or result.get("total_dividends_applied"),
            "cash_credited": result.get("total_cash_credited"),
        },
    )
    await db.commit()
    return result


from pydantic import BaseModel


class KillSwitchRequest(BaseModel):
    halted: bool
    reason: str | None = None


@router.get("/kill-switch")
async def get_kill_switch(
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    halted, reason = await is_halted(db)
    return {"halted": halted, "reason": reason}


@router.post("/kill-switch")
async def update_kill_switch(
    req: KillSwitchRequest,
    current_user=Depends(get_current_user),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin-only global trading halt. When halted=True every trade-execution
    endpoint returns 503. Changes take effect within 30 seconds across processes.
    """
    await set_halted(db, req.halted, req.reason, current_user.id)
    await log_audit(
        db,
        event_type="KILL_SWITCH_SET" if req.halted else "KILL_SWITCH_CLEARED",
        user_id=current_user.id,
        details={"reason": req.reason},
    )
    await db.commit()
    return {"halted": req.halted, "reason": req.reason}


class TestEmailRequest(BaseModel):
    to: str


@router.post("/email/test")
async def test_email(
    req: TestEmailRequest,
    current_user=Depends(get_current_user),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Send a diagnostic email to `to`. Verifies SMTP config end-to-end.

    Dev mode (SMTP_HOST empty): logs to stdout and returns captured=True.
    Prod: actually sends via aiosmtplib; 500 on failure with the real error.
    """
    from ...services.email_service import send_email
    try:
        await send_email(
            to=req.to,
            subject="DirectIndex Pro — SMTP test",
            text=(
                "This is a diagnostic message from the DirectIndex Pro admin "
                "console. If you're reading it, SMTP is wired up correctly.\n\n"
                f"Triggered by user_id={current_user.id} at {datetime.utcnow().isoformat()}Z"
            ),
        )
    except Exception as exc:
        raise HTTPException(500, f"SMTP send failed: {exc}")
    from ...config import settings as _s
    await log_audit(
        db, event_type="EMAIL_TEST_SENT",
        user_id=current_user.id,
        details={"to": req.to, "dev_mode": not bool(_s.SMTP_HOST)},
    )
    await db.commit()
    return {
        "sent": True,
        "dev_mode": not bool(_s.SMTP_HOST),
        "to": req.to,
    }


@router.post("/backup/run")
async def run_backup_now(
    current_user=Depends(get_current_user),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manual pg_dump → S3. Also runs nightly at 02:00 UTC."""
    try:
        r = await run_backup()
    except Exception as exc:
        raise HTTPException(500, f"Backup failed: {exc}")
    await log_audit(
        db, event_type="BACKUP_RUN",
        user_id=current_user.id,
        details={"status": r.get("status"), "key": r.get("key"), "size_bytes": r.get("size_bytes")},
    )
    await db.commit()
    return r


@router.get("/metrics")
async def metrics(
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Founder-visibility dashboard: users, portfolios, plans, harvests, MRR, audit rate."""
    return await collect_metrics(db)


@router.post("/harvest-notify/run")
async def run_harvest_notify_now(
    current_user=Depends(get_current_user),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manual trigger for the nightly harvest-opportunity email sweep."""
    r = await scan_and_notify_all(db)
    await log_audit(
        db, event_type="HARVEST_NOTIFY_SWEEP_RUN",
        user_id=current_user.id,
        details={"sent": r["sent"], "skipped": r["skipped"], "errors": r["errors"]},
    )
    await db.commit()
    return r


@router.get("/backup/list")
async def list_backups_endpoint(
    _admin=Depends(require_admin),
):
    return {"backups": await list_backups()}


@router.post("/retention/sweep")
async def run_retention_sweep(
    current_user=Depends(get_current_user),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manual trigger for the SEC 204-2 retention rotation. Also runs daily at 03:00 UTC."""
    result = await retention_sweep(db)
    await log_audit(
        db, event_type="RETENTION_SWEEP_RUN",
        user_id=current_user.id,
        details={
            "total_archived": result["total_archived"],
            "total_purged": result["total_purged"],
        },
    )
    await db.commit()
    return result


@router.post("/dr-drill/run")
async def run_dr_drill_now(
    current_user=Depends(get_current_user),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger a disaster-recovery drill on demand.

    Runs the same workflow as the auto-scheduled quarterly drill: fetch the
    latest S3 backup, restore it into the DR_DRILL_TARGET_URL scratch DB,
    count rows in core tables, emit a DR_DRILL_RUN audit event.

    Returns `status="skipped"` cleanly if backups or the scratch target
    aren't configured — 503 is only for real failures. See `docs/dr_runbook.md`.
    """
    try:
        r = await run_dr_drill(db=db)
    except Exception as exc:
        raise HTTPException(500, f"DR drill failed: {exc}")
    return r


@router.get("/dr-drill/history")
async def dr_drill_history(
    limit: int = Query(20, ge=1, le=200),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return the most recent DR drill runs from the audit log."""
    from sqlalchemy import select, desc
    from ...models.models import AuditEvent
    rows = (await db.execute(
        select(AuditEvent)
        .where(AuditEvent.event_type == "DR_DRILL_RUN")
        .order_by(desc(AuditEvent.created_at))
        .limit(limit)
    )).scalars().all()
    return {"runs": drill_history_from_audit(rows)}


@router.get("/constituents/status")
async def constituents_status(db: AsyncSession = Depends(get_db)):
    """
    Return per-index last_refreshed timestamp and active constituent count.
    """
    # TODO: auth
    status = []
    for idx in sorted(VALID_INDEXES):
        ts = await last_refreshed(db, idx)
        rows = await get_constituents(db, idx)
        status.append({
            "index": idx,
            "last_refreshed": ts.isoformat() if ts else None,
            "active_count": len(rows),
        })
    return {"status": status}
