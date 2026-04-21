"""
Disaster-recovery drill automation.

Runs a quarterly restore of the most recent backup into a scratch Postgres
database, verifies row counts across core tables, and emits a `DR_DRILL_RUN`
audit event with the result. This is the automated version of the manual
drill described in `docs/dr_runbook.md`.

Gating:
  - No-op in dev (SQLite or no `S3_BACKUP_BUCKET`) — returns `status="skipped"`.
  - Requires a separate scratch target URL via `DR_DRILL_TARGET_URL` env. We
    do NOT synthesize a database name off the production URL because that
    would risk pointing at prod by accident. Ops must create the scratch DB
    up front (one-time bootstrap).

Safety:
  - `pg_restore --clean --if-exists` drops the scratch schema before loading,
    so running back-to-back drills is safe.
  - We never write to production from this job. The scratch URL is the only
    target.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from ..config import settings
from .audit import log_audit

logger = logging.getLogger(__name__)

# Tables we always check row counts on. If any of these returns 0 (except on
# a brand-new restore where the backup is itself empty), that's a red flag.
CORE_TABLES = [
    "users",
    "portfolios",
    "positions",
    "tax_lots",
    "transactions",
    "trade_plans",
    "trade_plan_items",
    "audit_events",
    "recommendation_logs",
    "subscriptions",
    "corporate_action_logs",
]


def _dr_drill_target_url() -> str | None:
    return os.environ.get("DR_DRILL_TARGET_URL", "").strip() or None


def _is_postgres_target(url: str) -> bool:
    u = url.lower()
    return u.startswith("postgres")


def _latest_backup_key() -> str | None:
    """Find the newest backup object in S3. Returns None if no backups exist."""
    if not settings.S3_BACKUP_BUCKET:
        return None
    import boto3
    client = boto3.session.Session().client("s3", region_name=settings.AWS_REGION)
    prefix = settings.S3_BACKUP_PREFIX.rstrip("/") + "/"
    resp = client.list_objects_v2(
        Bucket=settings.S3_BACKUP_BUCKET, Prefix=prefix, MaxKeys=50,
    )
    rows = sorted(
        resp.get("Contents") or [],
        key=lambda r: r["LastModified"],
        reverse=True,
    )
    if not rows:
        return None
    return rows[0]["Key"]


def _download_backup(key: str, dest_path: str) -> int:
    import boto3
    client = boto3.session.Session().client("s3", region_name=settings.AWS_REGION)
    client.download_file(settings.S3_BACKUP_BUCKET, key, dest_path)
    return os.path.getsize(dest_path)


def _pg_restore(dump_path: str, target_url: str, jobs: int = 4) -> None:
    if not shutil.which("pg_restore"):
        raise RuntimeError("pg_restore not on PATH — install postgresql-client")
    cmd = [
        "pg_restore",
        "--dbname", target_url,
        "--clean", "--if-exists",
        "--no-owner", "--no-acl",
        "--jobs", str(jobs),
        dump_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_restore failed (rc={proc.returncode}): {proc.stderr[-2000:]}"
        )


def _row_counts(target_url: str) -> dict[str, int]:
    """Open a fresh psycopg2 connection against the scratch DB and count rows."""
    try:
        import psycopg2
    except ImportError as e:
        raise RuntimeError("psycopg2 is required for DR drill row counts") from e
    counts: dict[str, int] = {}
    with psycopg2.connect(target_url) as conn:  # type: ignore[attr-defined]
        with conn.cursor() as cur:
            for tbl in CORE_TABLES:
                try:
                    cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
                    counts[tbl] = int(cur.fetchone()[0])
                except Exception as e:  # missing table in an older backup
                    counts[tbl] = -1
                    logger.warning("DR drill: count failed for %s: %s", tbl, e)
                    conn.rollback()
    return counts


async def run_dr_drill(db=None) -> dict:
    """
    Execute one drill pass. Returns a status dict.

    Status values:
      - "skipped" — backups not configured, or scratch target not set
      - "ok" — restore succeeded and row counts look sane
      - "degraded" — restore succeeded but some tables came back empty /
        missing (might be fine on a truly fresh install)
      - "error" — restore or post-restore check failed; see `error`
    """
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    if not settings.S3_BACKUP_BUCKET:
        return {"status": "skipped", "reason": "S3_BACKUP_BUCKET not set"}

    target = _dr_drill_target_url()
    if not target:
        return {"status": "skipped", "reason": "DR_DRILL_TARGET_URL not set"}
    if not _is_postgres_target(target):
        return {"status": "skipped", "reason": "scratch target is not Postgres"}

    key = _latest_backup_key()
    if not key:
        return {
            "status": "skipped",
            "reason": f"no backups found under s3://{settings.S3_BACKUP_BUCKET}/{settings.S3_BACKUP_PREFIX}",
        }

    with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as tmp:
        tmp_path = tmp.name

    result: dict = {
        "status": "error",
        "started_at": started_at.isoformat(),
        "backup_key": key,
        "drill_target_db": target.split("/")[-1],  # db name only; no creds
    }

    try:
        size = _download_backup(key, tmp_path)
        result["backup_size_bytes"] = size
        _pg_restore(tmp_path, target)
        counts = _row_counts(target)
        result["restored_row_counts"] = counts
        # Degraded if any core table is -1 (didn't exist / couldn't count).
        # Empty counts are ok on a fresh DB — we don't flag 0 as a failure.
        result["status"] = "degraded" if any(v < 0 for v in counts.values()) else "ok"
    except Exception as e:
        result["error"] = str(e)[:500]
        logger.exception("DR drill failed")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        result["elapsed_seconds"] = round(time.perf_counter() - t0, 2)
        # Emit an audit event so history is queryable through the normal
        # audit log viewer. Best-effort: we don't blow up if the DB is down.
        if db is not None:
            try:
                await log_audit(
                    db,
                    event_type="DR_DRILL_RUN",
                    object_type="dr_drill",
                    details=result,
                )
                await db.commit()
            except Exception as e:
                logger.error("Failed to write DR drill audit event: %s", e)

    return result


def drill_history_from_audit(rows: list) -> list[dict]:
    """
    Transform a list of `AuditEvent` rows with event_type='DR_DRILL_RUN'
    into frontend-friendly summaries. Callers pass raw ORM rows; we only
    peek at `created_at` and the JSON blob.
    """
    out: list[dict] = []
    for row in rows:
        try:
            details = json.loads(row.details_json) if row.details_json else {}
        except (TypeError, json.JSONDecodeError):
            details = {}
        out.append({
            "id": row.id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "status": details.get("status"),
            "elapsed_seconds": details.get("elapsed_seconds"),
            "backup_key": details.get("backup_key"),
            "error": details.get("error"),
        })
    return out
