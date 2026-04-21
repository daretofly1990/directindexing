"""
Disaster-recovery restore script.

Downloads a pg_dump backup from S3 and restores it into a target Postgres
database. Run this out-of-band — during a real restore, the app should be
offline so no writes race the load.

Usage:
    python -m backend.scripts.restore_from_backup --list
    python -m backend.scripts.restore_from_backup --key pgdumps/direct-indexing-20260419T020000Z.dump \\
        --target-url postgresql://di:di_secret@db:5432/direct_indexing_restore

The restore database must exist and be empty — `pg_restore` will object
otherwise. Usual pattern is `createdb direct_indexing_restore` first, run
this script, verify, then repoint the app.

Quarterly drill: run this into a scratch database, check row counts, confirm
a handful of known rows deserialize, destroy the scratch DB.
"""
import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

import boto3

from backend.config import settings


def _parse_args():
    p = argparse.ArgumentParser(description="Restore a pg_dump backup from S3.")
    p.add_argument("--list", action="store_true", help="List recent backups and exit.")
    p.add_argument("--key", help="S3 object key of the dump to restore.")
    p.add_argument("--target-url", help="Postgres URL to restore into. Required unless --list.")
    p.add_argument("--bucket", default=settings.S3_BACKUP_BUCKET)
    p.add_argument("--region", default=settings.AWS_REGION)
    p.add_argument("--jobs", type=int, default=4, help="pg_restore parallel jobs.")
    return p.parse_args()


def list_backups(bucket: str, region: str, prefix: str = "") -> None:
    prefix = prefix or settings.S3_BACKUP_PREFIX
    client = boto3.session.Session().client("s3", region_name=region)
    resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=50)
    rows = sorted(resp.get("Contents") or [], key=lambda r: r["LastModified"], reverse=True)
    if not rows:
        print(f"No backups in s3://{bucket}/{prefix}")
        return
    for obj in rows:
        print(f"  {obj['LastModified'].isoformat()}  {obj['Size']:>12,}  {obj['Key']}")


def download_key(bucket: str, key: str, region: str, out_path: str) -> None:
    client = boto3.session.Session().client("s3", region_name=region)
    client.download_file(bucket, key, out_path)


def restore(dump_path: str, target_url: str, jobs: int) -> None:
    if not shutil.which("pg_restore"):
        raise RuntimeError("pg_restore binary not found on PATH. Install postgresql-client.")
    cmd = [
        "pg_restore",
        "--dbname", target_url,
        "--clean", "--if-exists",
        "--no-owner", "--no-acl",
        "--jobs", str(jobs),
        dump_path,
    ]
    print(f"Running: {' '.join(cmd[:-1])} <dump>")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise RuntimeError(f"pg_restore exited with code {proc.returncode}")
    print("Restore complete.")


def main():
    args = _parse_args()
    if not args.bucket:
        sys.stderr.write("S3_BACKUP_BUCKET not set and --bucket not passed.\n")
        sys.exit(2)

    if args.list:
        list_backups(args.bucket, args.region)
        return

    if not args.key or not args.target_url:
        sys.stderr.write("--key and --target-url are required unless --list is passed.\n")
        sys.exit(2)

    with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        print(f"Downloading s3://{args.bucket}/{args.key} -> {tmp_path}")
        download_key(args.bucket, args.key, args.region, tmp_path)
        print(f"Downloaded {os.path.getsize(tmp_path):,} bytes.")
        restore(tmp_path, args.target_url, args.jobs)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
