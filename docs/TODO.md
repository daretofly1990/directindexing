# Direct Indexing Platform — TODO v2

**Last updated:** 2026-04-20 (sixth push — ticker-change automation, DR drill auto-scheduling, Invite-your-CPA growth loop, load-test profile)

**Focus:** Phase 1 ships to self-directed individual investors. Phase 2 adds the RIA/advisor channel on the same codebase.

**Launch strategy:** Path B with a hedge toward Path C — finish Phase 1 code, then build Phase 2 during the 3–5 month regulatory wait, with parallel RIA customer discovery that can flip the launch order if early commitments materialize. Full plan in [launch_strategy.md](launch_strategy.md).

**Current operating mode: software-first.** The regulatory track (RIA registration, ADV filing, counsel, E&O insurance) is intentionally deferred to the final phase before launch. Engineering runs to completion first; the reg workstream kicks off only when the product is ready to ship and the 3–5 month ADV approval wait becomes the actual blocker. This is a deliberate trade-off — do not start any regulatory line item without an explicit decision to flip modes.

**Critical path (once software is done):** RIA registration is still required even without custody. Being a Registered Investment Adviser is triggered by *giving personalized securities advice for compensation*, not by holding client assets.

---

## Progress snapshot (2026-04-20 EOD)

**Shipped end-to-end:** M1 (individual persona + email verification), M3 (TradePlan lifecycle + broker exports + reconcile diff), M4 (compliance + encryption + retention rotation + marketing disclosures), M5 (dividends + corp actions + **ticker changes / class conversions**), M6 (AI safety + 21-case eval harness + structured citations + confidence/caveats + CI), M7 code-side (rate limiting / CORS / healthcheck / JSON logging / **quarterly DR auto-drill** / **load-test profile with SLO gating**), M2 frontend in full (onboarding wizard + plain-language dashboard + tooltips + tax PDF + audit log UI), M8 Stripe scaffolding, **M9 "Invite your CPA" growth loop** (signed magic-link tax-report share, 30-day TTL, revocable, audit-logged).

**Beyond original scope — five security hardening items shipped:**
- ✅ Idempotency keys on `sell`, `harvest`, `trade-plan/approve`, `trade-plan/mark-executed`, `rebalancing/execute`
- ✅ Global kill switch (`system_flags` table + `/api/admin/kill-switch` + 30s cache)
- ✅ Manual-sell dollar cap (30% of NAV, same threshold as AI plans)
- ✅ PII encryption via Fernet with key rotation (`EncryptedText` TypeDecorator) — live in `.env`, DB round-trip verified
- ✅ SlowAPI rate limiting migrated from the custom token-bucket (5/min + 20/hr on `/api/auth/token`, 3/min + 20/hr on `/api/signup/individual`)

**Test suite:** 102 passing — now includes a full end-to-end HTTP integration test exercising signup → ack → portfolio construct → find_losses → create plan → approve (with idempotency) → Schwab CSV export → reconcile upload (with diff report) → Form 8949 CSV. Also covers cross-user RBAC (403 on foreign portfolio) and ADV-not-accepted agent gating (403 until ADV Part 2A on file).

**Deploy-ready:** Dockerfile hardened (non-root user, `postgresql-client` installed for pg_dump/pg_restore, no reload mode). `fly.toml` and `render.yaml` configs plus step-by-step runbook at [docs/deployment.md](docs/deployment.md).

**Remaining real blockers for launch:**

*Software (small, code only):*
1. M5 return-of-capital / K-1 — still flagged for post-launch (rare in S&P 500). Ticker changes and class conversions now handled by `ticker_change_service.py` with admin endpoint + audit trail.
2. M6 CI: workflow file shipped, but branch-protection rules must be set in GitHub repo settings (no code can do that)
3. Load test — 500 concurrent users target — script is written (`backend/scripts/loadtest.py`, runbook at `docs/loadtest.md`), SLO enforcement wired, **still needs to be run against a deployed staging environment**

*External accounts / credentials (code paths are wired and 503 / stdout-fallback safely until set):*
4. M1 SMTP — paste `SMTP_HOST`+credentials in `.env` to stop printing verification emails to stdout. Provider-specific instructions are in `.env.example`.
5. M7 Sentry — create a project at sentry.io, paste `SENTRY_DSN`. PII scrubbing is pre-wired.
6. M7 AWS — `AWS_PROFILE` or `AWS_ACCESS_KEY_ID/SECRET`; set `S3_BACKUP_BUCKET` to turn on nightly pg_dump uploads; set `AWS_SECRETS_PREFIX` to start pulling secrets from Secrets Manager.
7. M7 DR drill — ✅ now auto-scheduled (1st of Jan/Apr/Jul/Oct at 04:00 UTC) via `backend/services/dr_drill.py`. **Requires `DR_DRILL_TARGET_URL` env var pointing at a scratch Postgres** — drill skips cleanly without it. Manual trigger: `POST /api/admin/dr-drill/run`; audit history: `GET /api/admin/dr-drill/history`. First manual run in staging still recommended before relying on the cron. Runbook updated at [docs/dr_runbook.md](docs/dr_runbook.md).
8. M7 Admin MFA — admin users enroll via `POST /api/auth/mfa/enroll` then confirm with `POST /api/auth/mfa/verify`. Code path is live and gating `/api/auth/token`.
9. M8 Stripe — create three products × two prices in the dashboard, paste six price IDs + secret + webhook secret in `.env`.

*Regulatory (do last — deferred until software is done):*
10. Regulatory track — RIA registration, ADV filing, counsel, E&O insurance. 3–5 month lead time. See bottom-of-file "Regulatory track" section.

---

## Pre-launch cutover checklist (flip these the day you go live)

**Full checklist lives at [docs/pre_launch_checklist.md](pre_launch_checklist.md) — 6 phases covering legal/content, pre-deploy secrets, deploy, post-deploy verification, go-live env flips, day-of monitoring, and the regulatory gate.**

The highlights below are the env-var flips specifically. For the complete cutover runbook (including what to rotate, what external accounts to create, and what to test before letting a real user sign up), use the full checklist document.

These are deliberately left at dev-safe defaults so nothing breaks during development. When billing is live and you're ready to ship to real retail customers, flip them in order:

- [ ] **Set `CLAUDE_MODEL_DEFAULT=claude-haiku-4-5`** in production `.env`. Keeps `CLAUDE_MODEL_PREMIUM=claude-opus-4-5`. Result: starter / standard / trial-lapsed / no-sub users route to Haiku (~20× cheaper output tokens), premium-tier subscribers keep Opus. Zero code change, just an env flip + restart. Drops API spend from ~$1.50/user/mo to ~$0.08/user/mo on the two cheap tiers. Per-tier routing is tested (`backend/tests/test_claude_model_routing.py`) and defaults are opus/opus so dev / staging / early-beta continues to behave identically.
- [ ] **Run the 21-case AI eval harness against Haiku** (`CLAUDE_MODEL_DEFAULT=claude-haiku-4-5 pytest backend/tests/test_ai_guardrails.py`) before the cutover to confirm the cheaper model still handles every guardrail edge case. If any fail, either gate that user tier to Opus or patch the prompt — do not ship Haiku on a failing eval.
- [ ] **Tighten `CORS_ORIGINS`** to the single production host (`https://yourdomain.com`) — no wildcards, no staging hosts.
- [ ] **Bump `SENTRY_TRACES_SAMPLE_RATE`** from 0.0 → 0.05 for real perf telemetry in prod.
- [ ] **Set `JSON_LOGS=1`** if the host aggregator expects structured logs (CloudWatch / Datadog / Loki).

---

## Phase 1 — Launch (self-directed only)

### Milestone 1 — Individual persona foundation ✅ SHIPPED

- [x] `individual` added to user role enum (`backend/models/models.py`)
- [x] `create_individual_user()` creates User + self-Client atomically (`backend/services/user_service.py`)
- [x] `assert_portfolio_access()` gates individuals to their self-client only (`backend/api/deps.py`)
- [x] `POST /api/signup/individual` — returns JWT for immediate continuation into onboarding
- [x] `Household` model + `household_wash_sale.py` helper for cross-account wash-sale scoping
- [x] Persona-aware `/api/me` returns `role`, `self_client`, `missing_acknowledgements`, `features` dict
- [x] **Email verification flow** — `backend/services/email_service.py` with JWT tokens (24h TTL), `GET /api/auth/verify-email`, `POST /api/auth/resend-verification`. Multipart text + HTML (templated welcome email), aiosmtplib async send, port-465 SMTPS support, in-memory capture hook for tests. Fires on signup; falls back to stdout log if `SMTP_HOST` is unset.
- [x] Admin test-email endpoint (`POST /api/admin/email/test`) surfaces SMTP config errors immediately rather than on first signup
- [x] Provider quick-setup docs inline in `.env.example` (SendGrid / Postmark / Mailgun / AWS SES / Gmail)
- [ ] Real SMTP credentials (ops task — set `SMTP_HOST` and friends in `.env`)

### Milestone 2 — Frontend UI gap-fill ✅ SHIPPED

- [x] CSV lot import screen — file picker, broker auto-detect indicator, first-50-row preview, commit/cancel
- [x] **Onboarding wizard** — 4-step modal (verify email → review acks → fund + sector exclusions → first scan) auto-opens for fresh individual users
- [x] **Plain-language dashboard** — hero banner with portfolio story, "what to do next" action block (verify email / scan for losses / rebalance cash drag), renamed metric labels to plain English
- [x] TLH opportunities view — sortable table with lot drill-down, ST/LT badge, wash-sale flag
- [x] Spec-ID lot picker — checkboxes per lot with override price + confirmation
- [x] `POST /api/portfolios/{id}/positions/{pos_id}/sell` — full flow including kill-switch gate, sell cap, idempotency, audit event
- [x] AI harvest advisor UI — reasoning steps rendered inline, guardrail warnings shown, "Save as Trade Plan" button, confidence + caveats visible
- [x] Draft trade list review — items table, approve/cancel actions, Schwab/Fidelity/generic CSV export
- [x] Tax report CSV download — realized-gains summary + Schedule D CSV + Form 8949 CSV
- [x] **Tax report PDF** — Form 8949 layout via reportlab, cover page with summary + disclosure, ST/LT detail pages
- [x] Post-trade CSV re-upload reconcile with **diff report** (partial fills, missed orders, unexpected symbols flagged)
- [x] **Educational tooltips** — `?` hover pills on: Tax-Loss Harvesting, Open Tax Lots, Spec-ID, ST/LT Term, AI Advisor, Schedule D/8949, Up-or-down, Cash-waiting. Styled with CSS-only tooltip boxes.
- [x] Admin console — user management, corporate actions panel (split/delist/spinoff/merger-cash), dividend trigger, kill switch, **TOTP enrollment with QR code**
- [x] **Audit log viewer UI** — dedicated admin tab with date-range filter + user filter, event table + recommendations table + JSON download of the exam-export bundle
- [x] **Plan & Billing tab** — three-tier pricing cards (Starter / Standard / Premium), monthly/annual toggle with 20% annual discount, feature bullets, value-math callout, start-trial buttons wired to Stripe Checkout, current-subscription status card, Stripe Customer Portal link, invoice history table with PDF download links

### Milestone 3 — Self-approval & trade-list export ✅ SHIPPED

- [x] `TradePlan` + `TradePlanItem` models with status enum (`DRAFT`/`APPROVED`/`EXECUTED`/`CANCELLED`/`EXPIRED`), `approved_at`, `executed_at`, `expires_at`, `recommendation_log_id`
- [x] Lifecycle endpoints — `POST .../trade-plans` (create), `/approve`, `/cancel`, `/mark-executed`, `/reconcile`
- [x] 24-hour expiration — enforced on approve; auto-flip to EXPIRED on stale draft
- [x] Broker CSV exporters — Schwab StreetSmart, Fidelity Active Trader Pro, generic (`backend/services/trade_export.py`)
- [x] Post-trade reconcile — uploads broker CSV, imports lots with overwrite, flips plan to EXECUTED
- [x] Idempotency + kill-switch gate on approve/mark-executed
- [x] **Reconcile diff** — `backend/services/reconcile_diff.py` snapshots pre-import share totals, compares against post-import CSV, flags `FILLED`/`PARTIAL`/`MISSED` per item and unexpected-symbol moves. Summary booleans (any_partial / any_missed / clean_fill) ride back in the reconcile response.
- [x] **Seed demo portfolio** — `backend/scripts/seed_demo.py` creates `demo@example.com / demo12345` with 20 curated positions (losers + LT gains + ST/LT crossover edge cases) and 3 historical harvest transactions. Idempotent; `--reset` rebuilds from scratch. Auto-accepts ack versions and marks email verified so the demo login flows through to a usable UI.

### Milestone 4 — Compliance & audit ◐ shipped except retention & marketing-rule

- [x] **Agent writes to `RecommendationLog`** — `harvest_agent` endpoint calls `log_recommendation()` with prompt, reasoning, tool_calls, draft_plan, model_version, prompt_version, adv_version_acknowledged, demo_mode
- [x] **AuditEvent on user-impacting actions** — wired for: MANUAL_SELL, HARVEST_EXECUTED, LOTS_IMPORTED, REBALANCE_EXECUTED, TRADE_PLAN_CREATED/APPROVED/CANCELLED/EXECUTED, ACKNOWLEDGEMENT_ACCEPTED, USER_CREATED, PORTFOLIO_CONSTRUCTED, CORP_ACTION_{split,delist,spinoff,merger_cash}, DIVIDEND_APPLIED, DIVIDEND_SWEEP_RUN, KILL_SWITCH_SET/CLEARED
- [x] **`FIELD_ENCRYPTION_KEYS` enabled** — live key in `.env`, applied to `Transaction.notes`, `RecommendationLog.{prompt,reasoning,tool_calls_json,draft_plan_json}`, `AuditEvent.details_json`. DB round-trip test proves ciphertext-at-rest.
- [ ] **Back up the encryption key to a real secrets manager** — currently only in local `.env` (ops task)
- [ ] **7-year retention policy** — archive table + scheduled rotation per SEC Rule 204-2 (schema exists, rotation job not written)
- [x] Form ADV Part 2A brochure delivery gate — enforced on `/harvest-agent`; returns 403 with acknowledgement CTA until individual has accepted
- [x] `Acknowledgement` model — document_type, version, accepted_at, ip_address; backs the ToS / ADV 2A / Privacy gate
- [x] Per-client compliance export — `GET /api/compliance/exam-export?start=X&end=Y&user_id=Z` returns JSON bundle of recommendations, audit events, transactions (admin only)
- [x] Form 8949 CSV — `/portfolios/{id}/form-8949.csv` with wash-sale "W" codes
- [x] **7-year retention rotation** — `backend/services/retention.py` moves >2yr rows to `*_archive` tables and purges >7yr archive rows. Daily cron at 03:00 UTC + `POST /api/admin/retention/sweep` for manual runs.
- [x] **Marketing rule compliance** — `backend/services/disclosures.py` with Rule 206(4)-1-compliant text auto-attached to backtest responses (with `performance_type: hypothetical`) and agent responses.
- [x] **Annual Reg S-P re-acceptance** — `user_has_accepted()` now enforces a 365-day freshness window for `privacy` and `adv_part_2a`; stale acknowledgements show up in `/api/acknowledgements/required` with `reason: "annual_reaccept"` so the frontend ack gate auto-reopens the dialog at the anniversary. `ToS` is excluded from the reaccept set (requires re-accept only on version bumps).
- [ ] **Communications archival** — RecommendationLog covers the advisor prompt+reasoning. Future chat-style interactions would need a separate `AdvisorMessage` table.

### Milestone 5 — Data quality ◐ core shipped; ROC / K-1 / FX remain

- [x] Dividend tracking — `finnhub_client.get_dividends()`, `dividend_service` (idempotent per `ex-date` marker), 06:30 UTC daily scheduled job, `Transaction(type="DIVIDEND")` + credit to `portfolio.cash`, `DIVIDEND_APPLIED` audit event
- [x] Spin-offs — `process_spinoff()` with basis allocation split + preserved purchase_date (IRS rule)
- [x] Cash mergers — `process_merger_cash()` closes open lots at cash_per_share
- [x] Delistings — `Position.is_delisted` + `process_delisting()`
- [x] **Ticker changes / class conversions** — `backend/services/ticker_change_service.py` with `process_ticker_change(old_symbol, new_symbol, ex_date, notes)`. Renames active positions; collision case re-parents lots to the existing target and recomputes weighted-average cost basis (old position marked `is_active=False` for audit). Idempotent via `CorporateActionLog(new_symbol, ex_date, "ticker_change")`. Admin endpoint `POST /api/admin/corporate-actions/ticker-change` with full audit event. Test coverage in `test_ticker_change.py` (5 cases including collision-merge weighted-basis math).
- [ ] **Cash tenders / return-of-capital** — separate ROC handler still needed
- [ ] **K-1 partnerships / REIT ROC** — flag as out of scope v1; rare in S&P 500
- [ ] **Currency** — all math assumes USD

### Milestone 6 — AI quality & safety ✅ SHIPPED

- [x] Hard guardrails — `ai_guardrails.apply_guardrails()` enforces SI block list, wash-sale flag on recent-buy symbols, 30%-of-NAV daily sell cap
- [x] Schema validation — `validate_draft_plan_schema()` rejects malformed agent output before persisting
- [x] Prompt + model versioning — `PROMPT_VERSION` and `MODEL_VERSION` constants recorded on every `RecommendationLog`
- [x] **Eval harness expanded to 21 cases** — covers: SI symmetry, SI replacement stripping, wash-sale flags (single + multi-symbol), max-sell cap triggers + safe sales, schema validation (valid/invalid/non-dict/missing fields/non-object-in-list/empty), embedded-gain exclusion, low-basis long-held lot preference, ST→LT boundary (day 364/365/366), post-sale wash-sale window blocking, non-SI replacement passthrough, exact-symbol repurchase block, empty plan, structured-string warnings, no-quotes graceful skip, household-scope wash-sale across sibling portfolios.
- [x] **Reasoning transparency** — `backend/services/reasoning_builder.py` attaches per-sell `citations` (lot_id, cost_basis, purchase_date, holding_days, is_long_term, loss_pct, selection_reason) and per-buy `selection_reason`. Wired into the agent route after guardrails.
- [x] **Confidence + caveats** — every sell gets `confidence: high|medium|low` plus `caveats[]` when loss is shallow (<3% from break-even), ST→LT crossover is near (<30 days), or wash-sale proximity flags the trade.
- [x] **CI regression** — `.github/workflows/ci.yml` runs the full test suite AND a dedicated `ai-eval` job that only runs `test_ai_guardrails.py` — wire branch protection to require both.
- [ ] **Enable live agent** — requires user to set their real `ANTHROPIC_API_KEY` in `.env` (demo mode still active)

### Milestone 7 — Production hardening ✅ code-complete; ops items need credentials

- [x] Rate limiting — SlowAPI on `/api/auth/token` (5/min + 20/hr) and `/api/signup/individual` (3/min + 20/hr)
- [x] Structured JSON logging — `logging_config.py`, enabled via `JSON_LOGS=1`
- [x] Healthcheck exercising deps — `/api/healthz` + `/health/deep` probe DB, Finnhub, Anthropic (1-token live ping via Haiku 4.5 when key present), encryption config
- [x] CORS hardened — methods pinned to `GET/POST/PATCH/DELETE/OPTIONS`, headers to `Authorization/Content-Type`; wildcard origin triggers warning log
- [x] Security headers middleware — `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`. HSTS delegated to Caddy.
- [x] Finnhub quote batching — `get_multiple_quotes()` already parallelized via `asyncio.gather`; TLH scan uses it
- [x] **Secrets management** — `backend/services/secrets.py` — AWS Secrets Manager lookup (`<AWS_SECRETS_PREFIX>/<env_name.lower()>`) with env-var + default fallback, per-process cache. Honors standard boto3 credential chain. To enable in prod: set `AWS_SECRETS_PREFIX` and give the app IAM `secretsmanager:GetSecretValue` permission.
- [x] **Sentry** — `backend/observability.py` with PII scrubbing (authorization header + cookies + password/token/secret-like keys dropped). FastAPI + SQLAlchemy integrations. Empty `SENTRY_DSN` = disabled, no-op.
- [x] **Automated Postgres backups** — `backend/services/backup_service.py` runs `pg_dump --format=custom` nightly at 02:00 UTC, uploads to `s3://$S3_BACKUP_BUCKET/$S3_BACKUP_PREFIX` with SSE-AES256, prunes objects older than `S3_BACKUP_RETENTION_DAYS` (default 30). Skips cleanly for SQLite dev.
- [x] **Disaster recovery** — `backend/scripts/restore_from_backup.py` (downloads from S3, `pg_restore` with `--clean --if-exists`, parallel jobs). Runbook at `docs/dr_runbook.md` covers RTO/RPO targets, kill-switch-first procedure, quarterly drill criteria, and the "things that will bite you" list.
- [x] **Automated quarterly DR drill** — `backend/services/dr_drill.py` scheduled for 1st of Jan/Apr/Jul/Oct at 04:00 UTC. Downloads latest S3 backup, `pg_restore`s to `DR_DRILL_TARGET_URL` scratch DB, row-counts every core table, emits `DR_DRILL_RUN` audit event with status + elapsed seconds + restored_row_counts. Admin endpoints: `POST /api/admin/dr-drill/run` (on-demand) and `GET /api/admin/dr-drill/history?limit=N` (reads from audit log). Skips cleanly when bucket/target/postgres not configured.
- [x] **Admin MFA (TOTP)** — pyotp-backed. `POST /api/auth/mfa/enroll` returns otpauth URI + secret, `POST /api/auth/mfa/verify` confirms the app has it and enables MFA. Admin logins then require a valid 6-digit code in the `client_secret` OAuth2 form field; mismatch = 401. Disable requires a live code. Secret is encrypted at rest via `EncryptedText`. **Frontend enrollment UI**: Admin tab renders a QR code (qrcode-generator CDN), shows manual-entry key, 6-digit confirmation input. `/api/auth/me` now exposes `totp_enabled`.
- [x] Admin test-email endpoint — `POST /api/admin/email/test {to}` sends a diagnostic through the real SMTP path; 500 on failure with the real error so config bugs surface.
- [x] Admin backup endpoints — `POST /api/admin/backup/run` + `GET /api/admin/backup/list` for on-demand backups and listing.
- [x] **Market-cap cache** — `backend/services/constituents.py` now maintains an in-process 24h cache (hit-ratio logged on every refresh). Separate from constituent-list reads, so a mid-week constituent add/drop doesn't trigger 500 fresh yfinance calls. `None` values are cached too (avoids re-fetching known-missing tickers).
- [x] **Load-test profile** — `backend/scripts/loadtest.py` Locust script. Per-virtual-user: signup → accept 3 acks → construct 200-position SP500 portfolio → weighted read-heavy task mix (10/5/3/2/1 across tax-lots / find-losses / realized-gains / harvest-agent / draft-trade-list). `@events.quitting` SLO enforcement: fail ratio > 0.5%, overall p95 > 3000ms, or harvest-agent p95 > 5000ms → non-zero exit (CI-gateable). Full runbook at [docs/loadtest.md](docs/loadtest.md).
- [ ] **Run the load test against deployed staging** — script is ready; needs a live staging deploy + 10-minute run + SLO verification. Ops task; no more code required.
- [ ] **Credentials setup** — see "External accounts / credentials" section at top of file. The code is live; these need real account values in `.env`.

### Deployment ✅ code-complete

- [x] Hardened Dockerfile — Python 3.12 slim, `postgresql-client` for pg_dump/pg_restore, non-root `app` user, no reload in prod, proxy-headers enabled for Fly/Render
- [x] `fly.toml` — app + managed Postgres attach, health check on `/health`, HTTPS forcing, auto-stop + auto-start
- [x] `render.yaml` blueprint — web service + managed Postgres, all secrets listed as `sync: false` so they're set in the dashboard
- [x] [docs/deployment.md](docs/deployment.md) runbook — credential checklist, per-platform steps, smoke test curls, post-deploy checklist, known gotchas
- [x] Postgres cross-DB SQL fixes — `WHERE 1=0` instead of `WHERE 0` in retention's `CREATE TABLE ... AS SELECT` so Postgres strict mode accepts it

**Beyond the original scope — five extra security items shipped:**
- [x] Idempotency keys — `IdempotencyRecord` table, header-based caching, applied to sell / harvest / trade-plan-approve / mark-executed / rebalance-execute
- [x] Kill switch — `SystemFlag` table + `/api/admin/kill-switch` endpoint + `assert_trading_enabled()` gate on every trade path + 30s in-memory cache
- [x] Manual-sell dollar cap — `check_manual_sell_cap()` enforces same 30%-of-NAV threshold as AI plans
- [x] PII encryption — Fernet `EncryptedText` TypeDecorator with multi-key rotation; live in `.env`; DB round-trip test proves `enc_v1:`-prefixed ciphertext at rest
- [x] SlowAPI migration — replaced the custom token-bucket with standard rate-limit decorators

### Milestone 8 — Billing ◐ scaffolding shipped; needs live Stripe keys

Tiers: $29 / $59 / $99 monthly with a 20% annual discount.

- [x] `Subscription` model (stripe_customer_id, stripe_subscription_id, tier, billing_cycle, status, trial_ends_at, current_period_end, cancel_at_period_end)
- [x] `backend/services/billing_service.py` — Checkout session creation with 14-day trial, customer portal session, webhook event handler covering `customer.subscription.{created,updated,deleted}` + `invoice.payment_failed` (dunning → past_due) + `invoice.paid` (recovery → active)
- [x] `POST /api/billing/checkout` + `POST /api/billing/portal` + `GET /api/billing/status` + `POST /api/billing/webhook` (webhook signature-verified, not JWT-gated — Stripe calls it directly)
- [x] Audit event `BILLING_CHECKOUT_STARTED` on checkout attempt
- [x] Alembic migration `004_subscriptions.py`
- [x] `.env.example` keys for `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, six price IDs, `STRIPE_TRIAL_DAYS`
- [x] **Pricing page** — `Plan & Billing` tab with three-tier cards, monthly/annual toggle with 20% annual discount, feature bullets, value-math callout ("$8K harvested × 37% bracket = $2,960 saved, pays for Premium for 30 months"), `Start free trial` buttons → `/api/billing/checkout`, current-subscription card, Stripe portal link.
- [x] **Invoice PDFs** — `GET /api/billing/invoices` pulls from Stripe's Invoice API, returns the `invoice_pdf` signed URL. UI renders a table with amount, status badge, and PDF download link.
- [ ] **Live Stripe account** — create three products (starter/standard/premium) × two prices (monthly/annual), paste the six price IDs into `.env`, then all endpoints switch from 503 to functional
- [ ] Dunning email copy (Stripe sends default templates; custom copy recommended)
- [ ] Three individual tiers (Starter / Standard / Premium) with portfolio-size ceilings
- [ ] Annual plan with 20% discount
- [ ] 14-day free trial, credit-card upfront
- [ ] Dunning, past-due suspension, reactivation
- [ ] Invoice PDFs
- [ ] Pricing page with tax-alpha value math

### Retention — harvest opportunity notifications ✅ SHIPPED

Triggers a daily email to individual users when their portfolio has >$500 of harvestable losses. Key retention mechanic for a subscription product — "the software earned its fee today."

- [x] `backend/services/notifications.py` — threshold-triggered scan, 7-day cooldown per user, opt-out via audit event (no new column needed)
- [x] `HARVEST_NOTIFICATION_SENT` / `HARVEST_NOTIFICATIONS_DISABLED` / `HARVEST_NOTIFICATIONS_ENABLED` audit event types
- [x] 21:00 UTC cron (post-market-close for US users) + manual trigger `POST /api/admin/harvest-notify/run`
- [x] HTML + plaintext multipart email with top-5 opportunity table, estimated savings callout, unsubscribe link
- [x] `GET /api/users/me/notifications` + `POST /api/users/me/notifications` opt-in/out API

### Admin metrics dashboard ✅ SHIPPED

Founder visibility tool (admin-only).

- [x] `backend/services/metrics_service.py` — users (total/active/new/verified/TOTP), portfolios, trade plans by status, harvests (lifetime + 30d), AI recommendations (24h + lifetime), subscriptions (active/trialing/past_due + MRR estimate), audit event rate
- [x] `GET /api/admin/metrics` single-payload endpoint
- [x] Frontend Metrics tab with grouped sections (Users / Revenue / Engagement / AI)

### Milestone 9 — Growth loops ◐ "Invite your CPA" shipped; others parked

Cheap alongside the UI, not blocking launch.

- [ ] Referral codes (one month free per converting referral)
- [x] **"Invite your CPA" flow** — signed magic-link share of a read-only tax-report view for the invited CPA. Natural Phase 2 RIA-channel funnel mechanic: CPAs see the product, CPAs refer high-net-worth clients to RIAs, inbound starts accruing before Phase 2 launches.
  - `CPAInvite` model + Alembic migration `006_cpa_invites.py` (indexed on user_id, portfolio_id, token_hash, created_at)
  - `backend/services/cpa_invite_service.py`: 30-day TTL JWT, SHA-256-hashed `jti` stored at rest (rotate-to-revoke), HTML+text email send via existing `email_service`, `build_cpa_view_payload()` returns sanitized summary (ST/LT split, wash-sale codes, closed-lot detail) + disclosure text
  - `backend/api/routes/cpa_invites.py`: authenticated `POST/GET/DELETE /api/cpa-invites` for the user, public `GET /api/cpa/view?token=X` for the CPA (mounted outside the global JWT group so the signed token IS the auth)
  - Audit events: `CPA_INVITE_SENT`, `CPA_INVITE_VIEWED` (bumps view_count + first/last_viewed_at), `CPA_INVITE_REVOKED`
  - Test coverage in `test_cpa_invite.py` (7 cases: happy path, invalid signature, expired, revoked, rotated-jti replay protection, payload shape, view counter increments)
- [ ] Annual "your year in tax alpha" branded PDF
- [ ] In-app NPS + churn survey at cancel
- [ ] SEO landing pages — "tax-loss harvesting calculator for Schwab accounts" and similar
- [ ] **Frontend UI for CPA invites** — API is live but no Invite-your-CPA button in the tax report tab yet

---

## Phase 2 — RIA channel (deferred)

Codebase should not paint itself into a corner. Same engine, same TLH primitives, same AI advisor — new UX and new pricing. Don't build now, but flag during Phase 1 design to keep options open.

- [ ] Re-introduce advisor-facing UX — client list, per-client dashboard, bulk harvest scans
- [ ] `TradePlan` approval workflow for non-discretionary RIAs (AI drafts → advisor reviews → client approves via email magic-link → advisor executes)
- [ ] Discretionary mode (advisor executes without per-trade client approval per client agreement)
- [ ] `Firm` entity and `firm_admin` role for multi-advisor shops
- [ ] White-label (logo, primary color, email sender, custom domain)
- [ ] SSO (SAML 2.0 / OIDC) — required by larger firms
- [ ] Per-seat + per-client billing tier (pricing doc: $299 Solo, $199+$20 Firm, bps-based Enterprise)
- [ ] SOC 2 Type I within 12 months of Phase 2 launch

**Forward-compatibility actions to take during Phase 1:**
- Use `Client.is_self` flag rather than forking schema
- Make `TradePlan.approver_user_id` nullable in anticipation of advisor-approved plans
- Keep `Portfolio.client_id` and `Client.advisor_user_id` relations intact even when unused
- All audit logs include `acting_user_id` separate from `target_client_id`

---

## Regulatory track — FINAL STEP BEFORE LAUNCH (intentionally deferred)

**Do not start these items yet.** This workstream is parked until the software is feature-complete and production-hardened. When we're ready to flip from build-mode to launch-mode, this track opens and adds ~3–5 months of calendar time before the product is legally shippable to retail customers. That is acknowledged and accepted.

**Why deferred:** RIA registration is expensive ($20–60K counsel + $5–15K/yr E&O) and time-boxed (once ADV is filed, it starts running). Paying for those during the build phase wastes runway. Starting it the day we're ready to ship means the reg wait is the only gate, not a concurrent distraction.

**Watch-for during the build:** if the product becomes demo-able earlier than expected, or if a friendly RIA offers to white-label and assume the reg overhead, flip this workstream on immediately.

- [ ] Securities counsel engaged ($20–60K initial setup)
- [ ] Entity formed (LLC or C-corp), EIN, operating agreement
- [ ] Form ADV Parts 1, 2A, 2B filed with IARD
  - State-by-state under $100M AUM equivalent (home state + any state with 6+ clients)
  - Internet Adviser Exemption worth discussing with counsel — fits a subscription-fee app with all advice delivered via interactive website
- [ ] Written compliance policies & procedures (Rule 206(4)-7)
- [ ] CCO designated (founder initially acceptable)
- [ ] Errors & omissions insurance ($5–15K/yr)
- [ ] Cybersecurity policy (Reg S-P, 2024 amendments)
- [ ] Client agreement template lawyer-reviewed
- [ ] Privacy policy and ToS lawyer-reviewed (not a template) — until then, the `Acknowledgement` gate uses placeholder copy
- [ ] Form ADV Part 2A brochure written — this is the document delivered at the M4 ADV gate
- [ ] Annual ADV amendment calendar set up

**Revised timeline:** software to production-ready state first (weeks 1–N with N tracked by the milestone checklist above), THEN file ADV, THEN 3–5 month state approval wait, THEN launch. No parallel track.

---

## What NOT to build

- **Broker execution** — STATUS.md is right to defer this. Trade-list export + CSV reconcile 