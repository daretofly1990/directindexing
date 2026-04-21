"""
SEC Rule 204-2 retention rotation.

Rule 204-2 requires RIAs to retain records for 5 years, with the first 2 years
in the office (readily accessible). We approximate that with two tiers:

  RECENT    — normal tables (recommendation_logs, audit_events, transactions)
  ARCHIVED  — same data moved to *_archive tables after 2 years
  PURGED    — deleted after 7 years (5 required + 2 safety margin)

`retention_sweep()` runs daily and moves rows between tiers. Archive tables
are same-schema append-only copies; restoring is a straight SELECT + re-insert
if an examiner asks.

For SQLite (dev) we use timestamp-based moves inside a transaction. For
production Postgres, the same code works; you can additionally move archive
rows to a different tablespace / slower storage if cost matters.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

ARCHIVE_AFTER_DAYS = 365 * 2      # 2 years
PURGE_AFTER_DAYS = 365 * 7        # 7 years total retention

# Tables that hold regulated records. Each gets a mirror *_archive table.
REGULATED_TABLES = [
    "recommendation_logs",
    "audit_events",
    "transactions",
]


async def _ensure_archive_table(db: AsyncSession, table: str) -> None:
    """
    Idempotently create `<table>_archive` with the same columns as `<table>`.
    Uses CREATE TABLE IF NOT EXISTS ... AS SELECT * WHERE 1=0 for portability.
    """
    await db.execute(text(
        f"CREATE TABLE IF NOT EXISTS {table}_archive AS "
        f"SELECT * FROM {table} WHERE 1=0"
    ))


async def _move_old_rows(
    db: AsyncSession, table: str, ts_column: str, cutoff: datetime,
) -> int:
    """Move rows older than cutoff from `<table>` to `<table>_archive`. Return count."""
    await _ensure_archive_table(db, table)
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {ts_column} < :cutoff"),
        {"cutoff": cutoff},
    )
    count = count_result.scalar() or 0
    if count == 0:
        return 0
    await db.execute(
        text(f"INSERT INTO {table}_archive SELECT * FROM {table} WHERE {ts_column} < :cutoff"),
        {"cutoff": cutoff},
    )
    await db.execute(
        text(f"DELETE FROM {table} WHERE {ts_column} < :cutoff"),
        {"cutoff": cutoff},
    )
    return count


async def _purge_from_archive(
    db: AsyncSession, table: str, ts_column: str, cutoff: datetime,
) -> int:
    await _ensure_archive_table(db, table)
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM {table}_archive WHERE {ts_column} < :cutoff"),
        {"cutoff": cutoff},
    )
    count = count_result.scalar() or 0
    if count == 0:
        return 0
    await db.execute(
        text(f"DELETE FROM {table}_archive WHERE {ts_column} < :cutoff"),
        {"cutoff": cutoff},
    )
    return count


async def retention_sweep(db: AsyncSession) -> dict:
    """
    One pass: archive >2yr rows, purge >7yr archive rows.

    Column conventions:
      - recommendation_logs.created_at
      - audit_events.created_at
      - transactions.timestamp
    """
    now = datetime.utcnow()
    archive_cutoff = now - timedelta(days=ARCHIVE_AFTER_DAYS)
    purge_cutoff = now - timedelta(days=PURGE_AFTER_DAYS)

    moved: dict[str, int] = {}
    purged: dict[str, int] = {}

    for table in REGULATED_TABLES:
        ts_col = "timestamp" if table == "transactions" else "created_at"
        try:
            moved[table] = await _move_old_rows(db, table, ts_col, archive_cutoff)
            purged[table] = await _purge_from_archive(db, table, ts_col, purge_cutoff)
        except Exception as exc:
            logger.error("Retention sweep failed for %s: %s", table, exc)
            moved[table] = -1
            purged[table] = -1

    await db.commit()
    result = {
        "archive_cutoff": archive_cutoff.isoformat(),
        "purge_cutoff": purge_cutoff.isoformat(),
        "archived": moved,
        "purged": purged,
        "total_archived": sum(v for v in moved.values() if v >= 0),
        "total_purged": sum(v for v in purged.values() if v >= 0),
    }
    logger.info(
        "Retention sweep: archived %d, purged %d across %d tables",
        result["total_archived"], result["total_purged"], len(REGULATED_TABLES),
    )
    return result
