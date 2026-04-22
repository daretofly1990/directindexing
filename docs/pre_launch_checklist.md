# Pre-launch cutover checklist

**This is the canonical "before we let a real retail customer sign up" checklist.** Everything else in `docs/TODO.md`, `docs/deployment.md`, and `docs/dr_runbook.md` is supporting detail; this is the gate.

Organized by phase. Do them in order. Don't skip. If an item seems redundant with something you did in staging — do it again for prod.

Owner: you. Updated: 2026-04-21.

---

## Phase 0 — Legal + content (blocks go-live of any retail user)

Can happen in parallel with everything below, but **no retail user clicks "I agree" until these are done**.

- [ ] **ToS lawyer-reviewed** — current [frontend/index.html `ACK_COPY.tos`](../frontend/index.html) is placeholder text I wrote, not counsel copy
- [ ] **ADV Part 2A brochure written + lawyer-reviewed** — this is the document delivered at the acknowledgement gate before any personalized recommendation. Regulatory requirement.
- [ ] **Privacy Notice lawyer-reviewed** — Reg S-P compliance. Same file, `ACK_COPY.privacy`.
- [ ] **Disclosure strings** in [backend/services/disclosures.py](../backend/services/disclosures.py) (`BACKTEST_DISCLOSURE`, `ADVISOR_DISCLOSURE`, `TAX_REPORT_DISCLOSURE`) reviewed against Rule 206(4)-1 language and counsel-approved
- [ ] **Marketing copy on the pricing page** — "at a 37% marginal bracket..." claim reviewed; unsupported performance claims removed

---

## Phase 1 — Pre-deploy (before the first `fly deploy`)

Each of these is a real change to `.env` or an external account. None can happen from code.

### Secrets — rotate every default

- [ ] **`JWT_SECRET`** — currently `change-me-in-production-use-a-long-random-string-32chars` in local `.env`. Generate fresh: `openssl rand -hex 32`. Set as `fly secrets set JWT_SECRET=...` — **NEVER commit**.
- [ ] **`ADMIN_PASSWORD`** — currently literally `changeme`. Generate a strong passphrase. Bootstrap admin account gets created on first boot with this password; rotate it again via UI after deploy.
- [ ] **`ADMIN_EMAIL`** — set to a real email you monitor, not `admin@example.com`.
- [ ] **`FIELD_ENCRYPTION_KEYS`** — a real Fernet key is in local `.env`. **Before prod:** save this key to your secrets manager (1Password, AWS Secrets Manager, Vault) as `direct-indexing/field_encryption_keys`. **LOSING THIS KEY = LOSING THE DATA.** Back it up twice.
- [ ] **`FINNHUB_API_KEY`** — verify it's on a paid plan if you expect real traffic. Free tier is 60 req/min; a single active user with 20 positions burns 20 reqs per portfolio-load.

### Services that need external accounts

- [ ] **Anthropic** — account has billing enabled + credits. Budget ~$1.50/user/month for opus, ~$0.08/user/month for haiku.
- [ ] **Anthropic spend cap + alert** — set a monthly budget limit in the Anthropic console AND a usage alert at 50% so a runaway loop or abusive user can't drain the account overnight.
- [ ] **Finnhub plan confirmation + usage alert** — confirm the plan covers expected traffic (free tier is 60 req/min; the market-cap cache + quote batching keeps well under this, but a regression could blow through it). Add a usage-threshold alert in the Finnhub dashboard.
- [ ] **Stripe** — three products created (Starter / Standard / Premium), each with monthly + annual prices (6 price IDs total). Webhook endpoint registered at `https://yourdomain.com/api/billing/webhook` with secret copied into `.env`.
- [ ] **Sentry** — project created, DSN set in `.env` as `SENTRY_DSN`. Set `SENTRY_ENVIRONMENT=production`.
- [ ] **AWS IAM** — role or key with: `secretsmanager:GetSecretValue` on `direct-indexing/*`; `s3:PutObject` + `s3:DeleteObject` + `s3:ListBucket` on the backup bucket.
- [ ] **S3 backup bucket** created, versioning on, server-side encryption enforced, public access blocked. Paste bucket name into `S3_BACKUP_BUCKET`.
- [ ] **DR scratch Postgres** — second managed DB (smaller tier is fine), URL pasted into `DR_DRILL_TARGET_URL`. Quarterly drill writes here.
- [ ] **Managed Postgres PITR enabled** — point-in-time recovery on the production DB. Our nightly `pg_dump` gives ≈ 24h RPO; PITR is the primary defense against a bad-migration scenario (RPO of minutes). Check the managed-DB console — Fly Postgres needs it enabled on the volume; Render + Supabase have it on by default on paid tiers.
- [ ] **SMTP** — Mailtrap live tier (or SendGrid/Postmark/SES). Sender domain verified + SPF/DKIM records added to your DNS. `SMTP_FROM` = a real verified sender address.
- [ ] **Domain** — purchased, DNS pointed at Fly/Render, Caddy (or Fly's built-in TLS) has a valid cert. Verify with `curl -I https://yourdomain.com/health`.
- [ ] **Mailtrap live domain DNS** — if using Mailtrap Send, verify DKIM + SPF + DMARC per Mailtrap's sending-domains UI, or emails go to spam.
- [ ] **Uptime monitor** — Better Stack / Pingdom / UptimeRobot pointed at `GET /health/deep` (exercises DB + Finnhub + Anthropic + encryption config). Alert on `status != "ok"` to Slack/email/SMS with escalation.
- [ ] **Log shipping** — pipe stdout from Fly/Render to a searchable aggregator (CloudWatch, Better Stack Logs, Grafana Cloud, Axiom). JSON logging is already enabled via `JSON_LOGS=1`; the host just needs a drain configured.

### Env vars with dev-safe defaults that must change for prod

- [ ] **`APP_BASE_URL`** — from `http://localhost:8000` to `https://yourdomain.com`. Verification email links embed this.
- [ ] **`CORS_ORIGINS`** — tighten to the single production host. No wildcards, no `http://`, no localhost.
- [ ] **`DATABASE_URL`** — `sqlite+...` → `postgresql+asyncpg://...`. Fly Postgres attach does this automatically; Render's Blueprint does too. Verify with `fly ssh console -C "printenv DATABASE_URL"`.
- [ ] **`JSON_LOGS=1`** — if your log aggregator wants structured JSON.
- [ ] **`SENTRY_TRACES_SAMPLE_RATE=0.05`** — from 0.0. Gives real perf telemetry without crushing the Sentry quota.
- [ ] **`APP_VERSION`** — bump on every deploy so Sentry groups errors by release.

---

## Phase 2 — Deploy (the actual cutover)

Follow [docs/deployment.md](./deployment.md) for the platform-specific commands. High-level:

- [ ] `fly deploy` / Render blueprint apply succeeds
- [ ] `alembic upgrade head` runs cleanly (check the release logs)
- [ ] `GET /health` returns `{"status":"ok"}`
- [ ] `GET /api/healthz` returns `"database": "ok"`, `"finnhub": "ok"`, `"anthropic": "ok"` (or "unconfigured" if you skipped it), `"encryption": "ok"`
- [ ] First admin login via `POST /api/auth/token` succeeds — proves JWT + bcrypt + DB seed all work end-to-end

**Caution:** delete `direct_indexing.db` (the SQLite file) from the repo working tree and make sure it is NOT in the Docker image. Otherwise a misconfigured deploy might fall back to the SQLite and serve stale dev data.

---

## Phase 3 — Post-deploy, pre-public (the app is up; no users yet)

Don't skip these. Every one of them has bitten a startup.

- [ ] **TOTP-enroll the real admin account** via the UI immediately. Until you do this, `ADMIN_EMAIL` + `ADMIN_PASSWORD` is the only factor. Admin tab → "Enable 2FA" → scan QR → verify.
- [ ] **Rotate the admin password** via the UI after TOTP is enrolled. The `ADMIN_PASSWORD` env var was used once for bootstrap and can now be replaced or removed.
- [ ] **Run the first manual DR drill** via `POST /api/admin/dr-drill/run` against the scratch DB. Confirm status=`ok`, row counts reasonable. If it fails, fix before the auto-cron fires in production.
- [ ] **Run the load test** (`backend/scripts/loadtest.py`) against staging. 500 concurrent users, ~200 positions each, target <3s harvest scan. Don't run it against prod.
- [ ] **Send a test email** — `POST /api/admin/email/test {to: "<your email>"}`. Verify it arrives in the real inbox, not spam. If it lands in spam, DKIM/SPF isn't right.
- [ ] **Trigger a test Stripe checkout** with the Stripe dashboard in test-mode. Walk through signup → checkout → webhook arrival in the Stripe Events log → subscription row appears in the DB. Then cancel and confirm webhook flips the row.
- [ ] **Seed a production admin-safe demo portfolio** using `seed_demo.py` **but with a different email + strong password** than the defaults. The defaults (`demo@example.com` / `demo12345`) must NOT ship to prod.
- [ ] **First nightly backup** confirmed — check the S3 bucket the next morning for `pgdumps/direct-indexing-YYYYMMDDT020000Z.dump`.
- [ ] **First harvest-notification sweep** confirmed — 21:00 UTC log line: `Harvest-opportunity notify: sent=N, skipped=N, errors=0`.
- [ ] **Kill-switch end-to-end test** — `POST /api/admin/kill-switch` to enable, confirm every trade path returns 503 (sell, harvest, trade-plan approve, mark-executed, rebalance execute, harvest-agent), `DELETE /api/admin/kill-switch` to disable, confirm paths resume. If this is broken, the "stop everything" button doesn't work when it's most needed.
- [ ] **Scale web workers to match load-test sizing** — prod instances should match what the load test validated against on staging. If you passed the load test on 2× 512MB instances, ship prod on at least 2× 512MB (ideally with headroom). Staging and prod drifting in sizing is how load tests stop predicting anything.
- [ ] **Scrub log lines for PII** — tail the logs during the smoke test and confirm prompt/reasoning/tool-call-json strings do NOT appear in plaintext in logs. They're encrypted in the DB; the log path should also not leak them.
- [ ] **Frontend Invite-your-CPA button** — the API is live (`POST /api/cpa-invites`, `GET /api/cpa/view?token=X`); the tax-report tab has no entry point UI yet. Soft-launch blocker, not hard-launch.

---

## Phase 4 — Go-live flips (model routing + cost controls)

These are the dev-safe-default → production-appropriate toggles. Once confident that Phases 1-3 are solid.

- [ ] **`CLAUDE_MODEL_DEFAULT=claude-haiku-4-5`** — starter / standard / trial-lapsed / no-sub users route to Haiku (~20× cheaper output tokens). Premium tier keeps `CLAUDE_MODEL_PREMIUM=claude-opus-4-5`. Drops API spend from ~$1.50/user/mo to ~$0.08/user/mo on the cheap tiers.
- [ ] **Re-run the 21-case AI eval harness against Haiku** before the flip: `CLAUDE_MODEL_DEFAULT=claude-haiku-4-5 pytest backend/tests/test_ai_guardrails.py`. **If any case fails, do not flip.** Either patch the prompt or keep that tier on Opus.
- [ ] **Branch protection** on GitHub — require the `test` and `ai-eval` workflows to pass before merging to `main`. Settings → Branches → Add rule.

---

## Phase 5 — Day-of monitoring (watch the first 24 hours)

- [ ] **Sentry dashboard** open in a tab. Any unhandled exception in the first 24 hours gets a human eye within 15 minutes.
- [ ] **Stripe dashboard** watched for webhook delivery failures (Stripe retries for 3 days; you want to fix config issues on day one)
- [ ] **`fly logs` / Render live log tail** running somewhere — look for 500s, spiking latency on Finnhub/Anthropic, anything marked `ERROR` or `CRITICAL`
- [ ] **`/api/admin/metrics`** snapshot hourly — users/day, harvests/day, MRR, audit events/day. Drift or stalling = a bug upstream.
- [ ] **Sign up as a real retail user** yourself (separate from the admin account). Click through: signup → verify email → acknowledge ToS/ADV/Privacy → onboarding wizard → portfolio construction → first harvest scan. Something breaks here every time; better you find it than the first real customer.
- [ ] **On-call + escalation path documented** — for a solo founder: a pager number on the Sentry alert + an uptime-monitor SMS + a documented "if the founder is unreachable, do X" note (e.g. the kill-switch URL + admin creds in a trusted person's 1Password vault). The app can be down without a person knowing; don't let that happen silently.

---

## Phase 6 — Regulatory (the FINAL gate)

**You cannot accept a paying retail customer until this is done.** Software is ready; this is the 3-5 month lead-time item.

See [docs/TODO.md](./TODO.md) "Regulatory track — FINAL STEP BEFORE LAUNCH" for the full checklist. Top of list:

- [ ] Securities counsel engaged
- [ ] Entity formed (LLC or C-corp), EIN, operating agreement
- [ ] Form ADV Parts 1, 2A, 2B filed with IARD
- [ ] Written compliance policies & procedures (Rule 206(4)-7)
- [ ] CCO designated
- [ ] E&O insurance in force
- [ ] Cybersecurity policy documented (Reg S-P 2024 amendments)
- [ ] ADV Part 2A brochure finalized + uploaded to the Acknowledgement gate

---

## What's NOT on this checklist (and why)

| Not here | Why |
|---|---|
| "Write more tests" | 126 passing is enough for v1. Real traffic will surface real bugs that tests wouldn't catch anyway. |
| "Add WAL archiving" | Nightly pg_dump gives 24h RPO, which is plenty for a tax-harvesting product. Revisit if a customer actually asks for sub-daily recovery. |
| "Migrate to Postgres" | Phase 1 covers this via `DATABASE_URL`. Not a separate item. |
| "Mobile app" | Explicitly deferred in TODO.md "What NOT to build." |
| "Tighten cache TTLs" | 5-minute TTL is correct for prod. No change needed. |
| "Multi-region deploy" | One region in the US is fine until >1K customers. |

---

## When an item moves out of this doc

A checklist item leaves this file in exactly two cases:

1. **Done** — tick the box, leave it here (stays as the historical record of cutover)
2. **Scope-cut** — moved to the "What's NOT on this checklist" table above with a one-line rationale

Anything else stays. If you find yourself wanting to delete an item because "it's probably fine," write down the rationale and move it to the NOT table instead.
