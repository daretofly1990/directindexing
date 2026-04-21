# Disaster Recovery Runbook

**Audience:** on-call engineer. Assume you've been paged at 02:30 local time and are looking at a dead production database.

## What you're restoring from

- Nightly pg_dump uploads to `s3://$S3_BACKUP_BUCKET/pgdumps/direct-indexing-YYYYMMDDTHHMMSSZ.dump` (02:00 UTC)
- Server-side encrypted with AES-256
- 30-day retention (older objects auto-pruned by the backup job)

Verify the latest backup exists before doing anything else:

```
python -m backend.scripts.restore_from_backup --list
```

## RTO / RPO targets

- **RPO (data loss):** up to 24 hours — pg_dump runs nightly. If prod writes between 02:00 UTC and incident time are business-critical, consider WAL archiving (not set up in v1).
- **RTO (time to restore):** target 1 hour. Most of it is pg_restore throughput against a fresh Postgres instance.

## Restore procedure

1. **Stop writes.** Put the kill switch in (calls to the API layer that mutate state will then 503).

   ```
   curl -X POST https://app.example.com/api/admin/kill-switch \
     -H "Authorization: Bearer $ADMIN_JWT" \
     -H "Content-Type: application/json" \
     -d '{"halted": true, "reason": "DR restore in progress"}'
   ```

2. **Provision a target database.** Don't restore over the live one.

   ```
   createdb -U postgres direct_indexing_restore
   ```

3. **Run the restore.**

   ```
   python -m backend.scripts.restore_from_backup \
     --key pgdumps/direct-indexing-<TIMESTAMP>.dump \
     --target-url postgresql://di:<PW>@db:5432/direct_indexing_restore \
     --jobs 8
   ```

   Expected runtime on a 5GB dump, 4-core target: ~10 minutes.

4. **Smoke-check.** Confirm row counts line up with your last metrics snapshot:

   ```
   psql $TARGET_URL -c "select count(*) from users;"
   psql $TARGET_URL -c "select count(*) from transactions;"
   psql $TARGET_URL -c "select count(*) from trade_plans;"
   psql $TARGET_URL -c "select max(created_at) from audit_events;"
   ```

   The `max(created_at)` should be at or after the backup timestamp.

5. **Cut over.** Point `DATABASE_URL` at the restored database, redeploy the API. Watch `/api/healthz` until `database: ok`.

6. **Clear the kill switch.**

   ```
   curl -X POST https://app.example.com/api/admin/kill-switch \
     -H "Authorization: Bearer $ADMIN_JWT" \
     -d '{"halted": false}'
   ```

7. **Post-incident.** File the incident report, and answer the question *why did the primary die.* If it's ransomware, do not restore until the attack vector is closed.

## Automated quarterly drill

The app auto-runs a drill on the 1st of January, April, July, and October at
04:00 UTC. Configuration:

- `S3_BACKUP_BUCKET` must be set (so there's something to restore).
- `DR_DRILL_TARGET_URL` must point at a scratch Postgres database you've
  pre-created (`createdb direct_indexing_dr_drill`). **Do not set this to
  the production URL.** The scratch DB is wiped every run via
  `pg_restore --clean --if-exists`.
- The container needs `pg_restore` on PATH (installed via `postgresql-client`
  in the Dockerfile) and `psycopg2` at import time.

Result of each drill is recorded as a `DR_DRILL_RUN` audit event with
status, elapsed_seconds, backup_key, and restored row counts. View the
last 20 runs at `GET /api/admin/dr-drill/history` or through the Audit Log
tab filtered by event type.

On-demand trigger from the admin console:

```
curl -X POST https://app.example.com/api/admin/dr-drill/run \
  -H "Authorization: Bearer $ADMIN_JWT"
```

Auto-drill pass/fail: `status="ok"` with row counts matching expectations
within the backup's age window. `status="degraded"` means the restore
worked but some core tables were missing — check migration drift. `status="error"`
opens a P2 (not P1 — production isn't down, but the DR path is).

## Manual restore drill (fallback)

Block a Friday afternoon. Steps:

1. Spin up a scratch Postgres (RDS snapshot, docker-compose locally, whatever — don't use prod).
2. Run `--list`, pick the most recent nightly.
3. Run `--target-url` against the scratch DB.
4. Verify row counts and a handful of known rows deserialize cleanly. Do one full agent-log round-trip if encryption is enabled (decrypt an encrypted column — if decryption fails, the key wasn't in the secrets manager, fix that).
5. Destroy the scratch DB.
6. Record the total wall time in the DR drill log. If it exceeded 1h, investigate throughput or increase --jobs.

**Pass/fail criteria:** drill passes if restore completes end-to-end with encrypted columns readable and audit events intact. Otherwise open a P1 and don't declare pass until the gap is fixed.

## Things that will bite you

- **Missing `pg_dump` / `pg_restore` on the runner.** The Dockerfile installs `postgresql-client`; a bare Python image will not. Use the container that runs the app.
- **Secrets rotated between backup time and restore time.** If `FIELD_ENCRYPTION_KEYS` dropped the key that encrypted the rows in the dump, decryption fails. The restored rows will read as `None` with a "no decryption key" log line. To recover, add the retired key back to `FIELD_ENCRYPTION_KEYS` (it can be second — encryption uses the first, decryption tries all).
- **Stale webhooks.** Stripe will retry webhooks during the outage. The idempotency on `customer.subscription.*` should handle this, but check `subscriptions` rows for duplicate state.
- **APScheduler doubled up.** If both the primary and the restored DB have schedulers running, you get duplicate dividend sweeps. Ensure the primary is truly offline (container dead, not just paused).
