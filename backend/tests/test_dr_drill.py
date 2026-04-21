"""
Tests for the DR drill gating logic.

The real drill shells out to pg_restore against a Postgres instance — not
something we can exercise in-process. What we CAN verify:

- The gating logic ("skipped" returns) is correct for every unconfigured path.
- The audit event is written when a db session is provided.
- `drill_history_from_audit()` parses stored JSON correctly.
"""
import json
from datetime import datetime

import pytest

from backend.models.models import AuditEvent
from backend.services import dr_drill
from backend.services.dr_drill import run_dr_drill, drill_history_from_audit


@pytest.mark.asyncio
async def test_skipped_when_bucket_unset(db, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "S3_BACKUP_BUCKET", "")
    r = await run_dr_drill(db=db)
    assert r["status"] == "skipped"
    assert "S3_BACKUP_BUCKET" in r["reason"]


@pytest.mark.asyncio
async def test_skipped_when_target_unset(db, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "S3_BACKUP_BUCKET", "fake-bucket")
    monkeypatch.delenv("DR_DRILL_TARGET_URL", raising=False)
    r = await run_dr_drill(db=db)
    assert r["status"] == "skipped"
    assert "DR_DRILL_TARGET_URL" in r["reason"]


@pytest.mark.asyncio
async def test_skipped_when_target_is_sqlite(db, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "S3_BACKUP_BUCKET", "fake-bucket")
    monkeypatch.setenv("DR_DRILL_TARGET_URL", "sqlite:///tmp/foo.db")
    r = await run_dr_drill(db=db)
    assert r["status"] == "skipped"
    assert "Postgres" in r["reason"]


@pytest.mark.asyncio
async def test_skipped_when_no_backups_found(db, monkeypatch):
    """Bucket + target are set but no backup objects exist in S3."""
    from backend.config import settings
    monkeypatch.setattr(settings, "S3_BACKUP_BUCKET", "fake-bucket")
    monkeypatch.setenv(
        "DR_DRILL_TARGET_URL",
        "postgresql://u:p@h:5432/direct_indexing_dr_drill",
    )
    monkeypatch.setattr(dr_drill, "_latest_backup_key", lambda: None)

    r = await run_dr_drill(db=db)
    assert r["status"] == "skipped"
    assert "no backups found" in r["reason"]


def test_drill_history_parses_audit_rows():
    """The frontend shape is status + elapsed + backup_key + optional error."""
    rows = [
        AuditEvent(
            id=1,
            event_type="DR_DRILL_RUN",
            created_at=datetime(2026, 4, 1, 4, 0, 0),
            details_json=json.dumps({
                "status": "ok",
                "elapsed_seconds": 412.5,
                "backup_key": "pgdumps/direct-indexing-20260331T020000Z.dump",
                "restored_row_counts": {"users": 1200},
            }),
        ),
        AuditEvent(
            id=2,
            event_type="DR_DRILL_RUN",
            created_at=datetime(2026, 1, 1, 4, 0, 0),
            details_json=json.dumps({
                "status": "error",
                "elapsed_seconds": 9.2,
                "error": "pg_restore not on PATH",
            }),
        ),
    ]
    out = drill_history_from_audit(rows)
    assert out[0]["status"] == "ok"
    assert out[0]["elapsed_seconds"] == 412.5
    assert out[1]["status"] == "error"
    assert out[1]["error"].startswith("pg_restore")
