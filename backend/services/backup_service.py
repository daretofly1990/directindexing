"""
Nightly Postgres backup → S3.

Runs `pg_dump` into a temp file, uploads to S3 with server-side encryption,
prunes objects older than `S3_BACKUP_RETENTION_DAYS`. Logs the resulting
object key so the DR restore script can find it.

Safe in dev: if `S3_BACKUP_BUCKET` is empty or the DB URL is not Postgres,
the job exits cleanly with status="skipped" instead of crashing the scheduler.

Requires the `pg_dump` binary on PATH; the Dockerfile should install it.
"""
import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

from ..config import settings

logger = logging.getLogger(__name__)


def _is_postgres() -> bool:
    url = (settings.DATABASE_URL or "").lower()
    return url.startswith("postgres") or "asyncpg" in url or "psycopg" in url


def _pg_dump_connect_string() -> str:
    """
    Convert SQLAlchemy URL (postgresql+asyncpg://u:p@h:port/db) into the
    plain libpq URL pg_dump understands (postgresql://u:p@h:port/db).
    """
    url = settings.DATABASE_URL
    if "+" in url.split("://")[0]:
        scheme_with_driver, rest = url.split("://", 1)
        scheme = scheme_with_driver.split("+")[0]
        return f"{scheme}://{rest}"
    return url


async def _run_pg_dump(target_path: str) -> None:
    if not shutil.which("pg_dump"):
        raise RuntimeError(
            "pg_dump binary not found on PATH — install postgresql-client in the container."
        )
    cmd = [
        "pg_dump",
        "--no-owner", "--no-acl",
        "--format=custom",
        "--file", target_path,
        _pg_dump_connect_string(),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed (rc={proc.returncode}): {stderr.decode(errors='ignore')[:500]}")


def _s3_client():
    import boto3
    return boto3.session.Session().client("s3", region_name=settings.AWS_REGION)


def _prune_old_backups(bucket: str, prefix: str, retention_days: int) -> int:
    """Delete backup objects older than retention_days. Returns delete count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    client = _s3_client()
    paginator = client.get_paginator("list_objects_v2")
    to_delete = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            if obj["LastModified"] < cutoff:
                to_delete.append({"Key": obj["Key"]})
    if not to_delete:
        return 0
    # delete_objects caps at 1000 per call
    deleted = 0
    for i in range(0, len(to_delete), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": to_delete[i:i+1000]})
        deleted += len(to_delete[i:i+1000])
    return deleted


async def run_backup() -> dict:
    """One backup pass. Returns status dict for the admin + cron caller."""
    if not settings.S3_BACKUP_BUCKET:
        return {"status": "skipped", "reason": "S3_BACKUP_BUCKET not set"}
    if not _is_postgres():
        return {"status": "skipped", "reason": "DATABASE_URL is not Postgres"}

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    prefix = settings.S3_BACKUP_PREFIX.rstrip("/") + "/"
    key = f"{prefix}direct-indexing-{ts}.dump"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".dump") as tmp:
        tmp_path = tmp.name

    try:
        await _run_pg_dump(tmp_path)
        size = os.path.getsize(tmp_path)
        client = _s3_client()
        with open(tmp_path, "rb") as fh:
            client.put_object(
                Bucket=settings.S3_BACKUP_BUCKET,
                Key=key,
                Body=fh,
                ServerSideEncryption="AES256",
                ContentType="application/octet-stream",
                Metadata={
                    "backup_type": "pg_dump_custom",
                    "app_version": settings.APP_VERSION,
                    "created_at_utc": datetime.utcnow().isoformat(),
                },
            )
        pruned = _prune_old_backups(
            settings.S3_BACKUP_BUCKET, prefix, settings.S3_BACKUP_RETENTION_DAYS,
        )
        logger.info(
            "Backup OK: s3://%s/%s (%d bytes), pruned=%d",
            settings.S3_BACKUP_BUCKET, key, size, pruned,
        )
        return {
            "status": "ok",
            "bucket": settings.S3_BACKUP_BUCKET,
            "key": key,
            "size_bytes": size,
            "pruned_older_than_days": settings.S3_BACKUP_RETENTION_DAYS,
            "pruned_count": pruned,
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def list_backups(limit: int = 50) -> list[dict]:
    if not settings.S3_BACKUP_BUCKET:
        return []
    client = _s3_client()
    prefix = settings.S3_BACKUP_PREFIX.rstrip("/") + "/"
    resp = client.list_objects_v2(
        Bucket=settings.S3_BACKUP_BUCKET, Prefix=prefix, MaxKeys=limit,
    )
    rows = []
    for obj in resp.get("Contents") or []:
        rows.append({
            "key": obj["Key"],
            "size_bytes": obj["Size"],
            "last_modified": obj["LastModified"].isoformat(),
        })
    rows.sort(key=lambda r: r["last_modified"], reverse=True)
    return rows
