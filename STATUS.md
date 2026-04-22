# Direct Indexing Platform — Build Status

**Last updated:** 2026-04-20 (refreshed against TODO.md v2)

## Current mode: software-first
Engineering runs to feature-complete and production-hardened before the regulatory
workstream starts. RIA registration (counsel, ADV filing, E&O insurance, compliance
policies) is intentionally deferred to the final step before launch — see
[docs/TODO.md](docs/TODO.md) "Regulatory track — FINAL STEP BEFORE LAUNCH" for the
rationale and checklist. Expect a 3–5 month gap between "software done" and
"legally shippable to retail."

## Pre-launch cutover checklist
**Definitive list of every secret to rotate, env var to flip, external account to
create, and verification step before a real retail user signs up:**
[docs/pre_launch_checklist.md](docs/pre_launch_checklist.md). Organized by phase
so nothing gets missed.

## What this is
A production-ready direct indexing platform for individual investors with fee-conscious
$500K–$2M portfolios. Users hold individual index constituents in their own brokerage
account, harvest tax losses, apply ESG screens, and rebalance to target weights. No
custody and no broker integration — users copy the draft trade list into their existing
brokerage and re-upload the fill CSV to reconcile. Phase 2 adds a parallel RIA/advisor
channel on the same codebase.

## Tech stack
- **Backend**: FastAPI + async SQLAlchemy ORM
- **Database**: SQLite (dev) / PostgreSQL (prod via asyncpg)
- **Auth**: JWT Bearer tokens, bcrypt passwords, `admin` / `advisor` / `individual` roles, email verification, admin TOTP MFA
- **Migrations**: Alembic (async-compatible), five revisions shipped
- **Scheduler**: APScheduler (constituent refresh, dividend sweep, retention sweep, nightly S3 backup, harvest-opportunity notifications, quarterly DR drill)
- **Market data**: Finnhub (live quotes + dividends) + yfinance (historical prices, market caps with 24h in-process cache)
- **AI advisor**: Anthropic claude-opus-4-5 tool-use loop with 21-case eval harness, hard guardrails, schema validation, prompt/model versioning, structured citations, confidence + caveats. Falls back to demo mode when `ANTHROPIC_API_KEY` is empty.
- **Billing**: Stripe Checkout + Customer Portal + webhooks, three-tier subscription (Starter / Standard / Premium), 14-day CC-upfront trial, 20% annual discount, dunning wired
- **Observability**: Sentry (PII-scrubbed), structured JSON logs, `/health` + `/health/deep` + `/api/healthz` deep probes (DB + Finnhub + Anthropic + encryption)
- **Security hardening**: PII encryption at rest (Fernet, rotation-capable), SlowAPI rate limiting, security headers middleware, kill switch, idempotency keys on every trade path, 30%-of-NAV manual-sell cap
- **Reverse proxy**: Caddy (HTTPS — `tls internal` in dev, Let's Encrypt in prod)
- **Frontend**: Single-page vanilla JS + HTML served by FastAPI, onboarding wizard, plain-language dashboard, admin console with TOTP QR, Plan & Billing tab, audit log viewer, metrics tab

## Credentials needed (all in .env; code paths degrade safely until set)
| Key | Purpose | Status |
|---|---|---|
| `FINNHUB_API_KEY` | Live quotes + dividends | ✅ Set |
| `JWT_SECRET` | Token signing | ✅ Set |
| `ADMIN_PASSWORD` | Bootstrap admin | ✅ Set |
| `FIELD_ENCRYPTION_KEYS` | PII column encryption | ✅ Set (local); back up to Secrets Manager before prod |
| `ANTHROPIC_API_KEY` | Claude AI advisor (live mode) | ❌ Empty — demo mode active |
| `DATABASE_URL` | Postgres URL in prod | SQLite default for now |
| `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` | Verification + harvest-notification email | ❌ Empty — stdout fallback |
| `SENTRY_DSN` | Error tracking | ❌ Empty — disabled |
| `S3_BACKUP_BUCKET` / `AWS_*` | Nightly pg_dump uploads + Secrets Manager | ❌ Empty — backup job skipped |
| `STRIPE_SECRET_KEY` + six price IDs + webhook secret | Billing | ❌ Empty — `/billing/*` returns 503 |

---

## What is fully built

### Core engine
- **Tax-lot engine** (`backend/services/lot_engine.py`) — FIFO / LIFO / HIFO / MIN_TERM / SPEC_ID selection, ≥365-day ST/LT classification, 30-day pre-sale wash-sale check + post-sale repurchase block, partial lot close splits open lots, wash-sale disallowance stamped and deferred into replacement basis
- **Portfolio service** (`backend/services/portfolio_service.py`) — CRUD, individual-aware access, S&P 500 construction with sector/symbol exclusions, live pricing via Finnhub
- **Backtest** (`backend/services/backtest_service.py`) — yfinance adjusted-close, SPY benchmark, 4-week TLH simulation with wash-sale tracking, batched 100-ticker downloads, hypothetical-performance disclosures attached to responses
- **ESG exclusions** (`backend/services/esg_service.py`)
- **Rebalancing** (`backend/services/rebalancing_service.py`) — drift vs target, internal rebalance trades
- **Index constituents** (`backend/services/constituents.py`, `constituent_store.py`) — S&P 500 / NASDAQ-100 / Russell 1000, Wikipedia scraping, yfinance market cap with **24h in-process cache (hit ratio logged)**, persisted to DB, daily 06:00 UTC refresh

### Auth & multi-user
- **Users** — three roles (`admin` / `advisor` / `individual`), email verification (JWT tokens, 24h TTL), bcrypt, admin bootstrap from env, `POST /api/signup/individual` returns JWT
- **Households** — `Household` model + `household_wash_sale.py` helper for cross-account wash-sale scoping (spouse + IRAs)
- **Clients** — per-advisor managed clients or a 1:1 self-client (`is_self=True`) for individuals, per-client tax rates
- **Access control** — `assert_portfolio_access()` on every portfolio-scoped route
- **Acknowledgements** — ToS / ADV Part 2A / Privacy gate, annual Reg S-P re-acceptance for `privacy` and `adv_part_2a`
- **Admin MFA** — pyotp TOTP, QR enrollment in UI, required on every admin login, secret encrypted at rest

### TLH advisory primitives
Five pure functions in `backend/services/tlh_tools.py`, each returning plain JSON:

| Function | What it does |
|---|---|
| `find_losses(...)` | Ranked loss opportunities with per-lot detail + replacement candidates |
| `simulate_sale(...)` | Spec-ID read-only simulation: proceeds, ST/LT gain, wash-sale risk |
| `check_wash_sale(...)` | Pre-sale risk + post-sale block + earliest safe repurchase date |
| `propose_replacement(...)` | ETF replacements from sector map + fund-family cross-reference |
| `draft_trade_list(...)` | Final sell+buy list with compliance checklist |

All five are exposed as REST endpoints at `/api/portfolios/{id}/tlh/*` and over the MCP server (`backend/mcp_server.py`).

### AI harvest advisor
- **Claude agent** (`backend/services/tlh_agent.py`) — claude-opus-4-5 tool-use loop, calls the five primitives autonomously
- **Guardrails** (`backend/services/ai_guardrails.py`) — SI block list, wash-sale flag on recent-buy symbols, 30%-of-NAV daily sell cap, schema validation, prompt + model versioning recorded on every `RecommendationLog`
- **Reasoning transparency** (`backend/services/reasoning_builder.py`) — per-sell `citations` (lot id, basis, purchase date, holding days, is_long_term, loss_pct, selection_reason) + per-buy selection_reason + `confidence: high|medium|low` + `caveats[]` for shallow losses / ST→LT crossover / wash-sale proximity
- **ADV gate** — `/harvest-agent` returns 403 with acknowledgement CTA until individual has accepted ADV Part 2A
- **Eval harness** — 21 cases (`test_ai_guardrails.py`) in a dedicated CI job
- **Demo mode** — when `ANTHROPIC_API_KEY` is empty, real primitives run with hardcoded top-3 logic

### Trade plan lifecycle (self-approval)
- `TradePlan` + `TradePlanItem` with status enum (`DRAFT`/`APPROVED`/`EXECUTED`/`CANCELLED`/`EXPIRED`), 24-hour expiration enforced on approve
- Lifecycle routes: create / approve / cancel / mark-executed / reconcile
- Broker CSV exporters — Schwab StreetSmart, Fidelity Active Trader Pro, generic
- Post-trade reconcile with **diff report** — `reconcile_diff.py` flags `FILLED`/`PARTIAL`/`MISSED` per item + unexpected symbols, returns summary booleans (`any_partial`/`any_missed`/`clean_fill`)
- Idempotency keys + kill-switch gate + 30%-of-NAV sell cap on approve / mark-executed / manual-sell / harvest / rebalance-execute

### CSV lot importer
- Auto-detects Schwab / Fidelity / generic
- Parses symbol, date acquired, quantity, cost per share
- Appends or replaces (`overwrite` flag)
- Routes: `POST /api/portfolios/{id}/import/lots` (commit) + `/preview` (first 50 rows dry run)

### Corporate actions
- Splits / reverse-splits — idempotent on `(symbol, ex_date)`
- Delistings — marks `Position.is_delisted` without auto-closing lots
- Spin-offs — basis allocation split, purchase_date preserved (IRS rule)
- Cash mergers — closes open lots at cash_per_share via `lot_engine.close_lots_by_ids`
- **Ticker changes / class conversions** — `ticker_change_service.process_ticker_change()` renames symbol across all affected `Position` rows (preserving TaxLot history via the FK), idempotent on `(old_symbol, new_symbol, ex_date)`, admin endpoint at `/admin/corporate-actions/ticker-change`
- All actions emit `CORP_ACTION_*` audit events and `CorporateActionLog` rows

### Dividend tracking
- `finnhub_client.get_dividends()` + `dividend_service` (idempotent per `ex-date` marker)
- 06:30 UTC daily scheduled sweep
- Creates `Transaction(type="DIVIDEND")` + credits `portfolio.cash`
- `DIVIDEND_APPLIED` audit event per symbol
- `DIVIDEND_SWEEP_RUN` audit event per sweep

### Compliance & audit
- **`RecommendationLog`** — every AI run is recorded with prompt, reasoning, tool_calls, draft_plan, model_version, prompt_version, adv_version_acknowledged, demo_mode
- **`AuditEvent`** — MANUAL_SELL, HARVEST_EXECUTED, LOTS_IMPORTED, REBALANCE_EXECUTED, TRADE_PLAN_{CREATED,APPROVED,CANCELLED,EXECUTED}, ACKNOWLEDGEMENT_ACCEPTED, USER_CREATED, PORTFOLIO_CONSTRUCTED, CORP_ACTION_{split,delist,spinoff,merger_cash,ticker_change}, DIVIDEND_APPLIED, DIVIDEND_SWEEP_RUN, KILL_SWITCH_SET/CLEARED, HARVEST_NOTIFICATION_SENT, HARVEST_NOTIFICATIONS_DISABLED/ENABLED, BILLING_CHECKOUT_STARTED, CPA_INVITE_SENT, CPA_INVITE_VIEWED, DR_DRILL_RUN
- **7-year retention rotation** — `retention.py` archives >2yr rows and purges >7yr rows; daily 03:00 UTC cron + manual `POST /api/admin/retention/sweep`
- **Marketing rule (206(4)-1)** — `disclosures.py` attaches compliant text to backtest + agent responses
- **Annual Reg S-P re-acceptance** — 365-day freshness window on `privacy` + `adv_part_2a` triggers ack gate reopening
- **PII encryption at rest** — Fernet `EncryptedText` TypeDecorator with multi-key rotation on `Transaction.notes`, all `RecommendationLog` encrypted fields, `AuditEvent.details_json`, `User.totp_secret`
- **Per-client exam export** — `GET /api/compliance/exam-export?start=X&end=Y&user_id=Z` returns a JSON bundle of recommendations, audit events, transactions
- **Form 8949 CSV** with wash-sale "W" codes; tax-report PDF with Form 8949 layout via reportlab

### Billing (Stripe)
- `Subscription` model + Alembic migration `004_subscriptions.py`
- `billing_service.py` — Checkout with 14-day trial, Customer Portal, webhook handler for `customer.subscription.{created,updated,deleted}` + `invoice.payment_failed` (past_due dunning) + `invoice.paid` (recovery)
- Routes: `POST /api/billing/checkout`, `POST /api/billing/portal`, `GET /api/billing/status`, `POST /api/billing/webhook` (signature-verified, not JWT-gated), `GET /api/billing/invoices`
- Frontend Plan & Billing tab with three-tier cards, monthly/annual toggle, value-math callout, current-subscription card, invoice history table with PDF download links

### Growth loops
- **Invite your CPA** — `POST /api/cpa-invites` creates a signed magic-link to a limited read-only tax-report view; invited CPAs see realized gains / 8949 rows / summary for the one portfolio and nothing else. Tracked as inbound for the Phase 2 RIA channel. CPA_INVITE_SENT + CPA_INVITE_VIEWED audit events.

### Retention — harvest opportunity notifications
- `notifications.py` — daily 21:00 UTC cron scans all individual portfolios for >$500 of harvestable losses, sends top-5 opportunity email with HTML + plaintext, 7-day per-user cooldown, opt-out endpoint with audit trail
- `GET`/`POST /api/users/me/notifications` opt-in/out API, `POST /api/admin/harvest-notify/run` manual trigger

### Production hardening
- **Rate limiting** — SlowAPI, 5/min + 20/hr on `/api/auth/token`, 3/min + 20/hr on `/api/signup/individual`
- **Structured JSON logging** — `logging_config.py`, toggle via `JSON_LOGS=1`
- **Healthchecks** — `/health`, `/health/deep`, `/api/healthz` probe DB + Finnhub + Anthropic (1-token Haiku ping when key present) + encryption config
- **CORS** — explicit method/header allowlist, wildcard origin logs a warning
- **Security headers middleware** — `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY` (HSTS on Caddy)
- **Secrets management** — `secrets.py` resolves from AWS Secrets Manager (prefix-scoped) with env-var + default fallback, per-process cache
- **Sentry** — PII scrubbing (authorization + cookies + password-like keys dropped), FastAPI + SQLAlchemy integrations
- **Automated backups** — nightly 02:00 UTC pg_dump to S3 with SSE-AES256, auto-prune at `S3_BACKUP_RETENTION_DAYS` (default 30)
- **Disaster recovery** — `restore_from_backup.py` + runbook at `docs/dr_runbook.md`; **quarterly DR drill auto-scheduled** (see below)
- **Finnhub quote batching** — `get_multiple_quotes()` via `asyncio.gather`

### Disaster recovery
- `backend/scripts/restore_from_backup.py` — downloads from S3, `pg_restore --clean --if-exists` with parallel jobs
- **Auto-drill** — APScheduler job runs quarterly (1st of Jan/Apr/Jul/Oct at 04:00 UTC): pulls latest backup, restores into a scratch database, checks row counts across core tables, emits `DR_DRILL_RUN` audit event with summary (status, restored_row_counts, elapsed_seconds, drill_target_url), drops scratch DB on success
- On-demand trigger: `POST /api/admin/dr-drill/run`
- List drill history: `GET /api/admin/dr-drill/history`

### Infrastructure & deploy
- Dockerfile hardened — Python 3.12 slim, `postgresql-client` for pg_dump/pg_restore, non-root `app` user, no reload in prod, proxy-headers enabled
- `fly.toml` (app + managed Postgres attach, `/health` probe, HTTPS forcing, auto-stop/start)
- `render.yaml` blueprint (web service + managed Postgres, secrets as `sync: false`)
- `docker-compose.yml` — postgres + api + caddy
- Caddyfile — HTTP→HTTPS redirect, `tls internal` (dev) / Let's Encrypt (prod)
- `docs/deployment.md` runbook with credential checklist, platform steps, smoke-test curls

### Tests
Full suite passes — integration + unit coverage of every code path:

```
test_ai_guardrails.py       # 21-case eval (shipped in M6)
test_annual_ack_and_cache.py
test_auth.py
test_constituents.py
test_corporate_actions.py
test_corporate_actions_v2.py # spinoff / merger-cash / delist / ticker-change
test_cpa_invite.py          # invite flow + magic-link view gate
test_dividend_service.py
test_email_service.py
test_encryption_db_roundtrip.py
test_integration_e2e.py      # signup → ack → construct → find_losses → plan → approve → export → reconcile → 8949
test_lot_engine.py
test_m7_ops.py               # backup / retention / MFA / kill switch / idempotency / DR drill
test_security_hardening.py
test_sell_service.py
test_trade_plan_service.py
test_user_service.py
```

Run: `pytest` from project root.

### Frontend
- Login overlay (email + password, JWT in localStorage)
- Auto-redirect to login on 401
- Onboarding wizard (4 steps) for fresh individual users
- Plain-language dashboard with "what to do next" action block
- CSV lot import screen (preview + commit)
- TLH opportunities table with lot drill-down, ST/LT badges, wash-sale flags
- Spec-ID lot picker
- AI harvest advisor UI (reasoning steps, citations, confidence, caveats)
- Draft trade list review with approve/cancel/export
- Tax report CSV + PDF download
- Plan & Billing tab (three-tier pricing cards, annual/monthly, invoices)
- Admin console — users, corporate actions (split/delist/spinoff/merger-cash/ticker-change), dividend trigger, kill switch, TOTP enrollment QR
- Audit log viewer (date + user filter, JSON export)
- Metrics tab (users / revenue / engagement / AI)
- Invite your CPA modal

---

## What is NOT built yet (priority order)

### Launch blockers (small)
1. M5 edges — cash tenders / return-of-capital, K-1 partnerships (rare in S&P 500, out of scope v1)
2. M6 branch protection — CI workflow shipped but repo-settings step requires GitHub UI action
3. Load test at 500 concurrent users / 200-position portfolios — script in `backend/scripts/loadtest.py`, not yet run against a staging deploy
4. Real SMTP credentials — currently stdout fallback; paste `SMTP_HOST`+creds in `.env`
5. Real Anthropic API key — currently demo mode; paste `ANTHROPIC_API_KEY` in `.env`
6. Real Stripe live keys + six price IDs + webhook secret — currently 503
7. Real AWS credentials + `S3_BACKUP_BUCKET` — currently nightly backup job skipped
8. Sentry DSN — currently Sentry disabled
9. Admin MFA enrollment — live, each admin must enroll via Admin → TOTP tab

### Intentionally NOT built
- **Broker execution** — no Alpaca / IBKR / Tradier. Customers won't ACATS from Schwab/Fidelity for a TLH tool; trade-list export + CSV reconcile is the chosen architecture. `draft_trade_list()` output is already shaped to feed a broker adapter if a future decision flips this.
- **K-1 partnerships / REIT ROC** — rare in S&P 500 scope, out of v1.
- **USD-only math** — no multi-currency.

### Phase 2 RIA channel (not yet started)
Codebase is forward-compatible (`Client.is_self` flag, nullable `TradePlan.approver_user_id`, `acting_user_id` separate from `target_client_id` in audit). Work items:
- Advisor-facing client list + per-client dashboard + bulk harvest scans
- Non-discretionary approval flow (AI drafts → advisor reviews → client approves via magic-link → advisor executes)
- Discretionary mode (advisor executes without per-trade client approval)
- `Firm` entity + `firm_admin` role
- White-label + SSO (SAML 2.0 / OIDC)
- Per-seat + per-client billing tier (Solo / Firm / Enterprise — see pricing_strategy.md)
- SOC 2 Type I within 12 months of Phase 2 launch

### Regulatory (deferred; do last)
RIA registration, Form ADV Parts 1/2A/2B filed with IARD, securities counsel, compliance
policies & procedures per Rule 206(4)-7, E&O insurance, cybersecurity policy (Reg S-P).
See TODO.md "Regulatory track" for the full checklist.

---

## File map (key files only)

```
backend/
  main.py                        # App entrypoint, scheduler, route registration, health probes
  config.py                      # Settings (pydantic-settings)
  auth.py                        # JWT + bcrypt + get_current_user + require_admin + TOTP gate
  database.py                    # SQLAlchemy async engine + session factory
  logging_config.py              # Structured JSON logs
  observability.py               # Sentry init with PII scrubbers
  rate_limit.py                  # SlowAPI wrapper
  models/models.py               # Every ORM model
  api/
    deps.py                      # assert_portfolio_access()
    routes/
      acknowledgements.py        # ToS / ADV / Privacy gate
      admin.py                   # Kill switch, backup list/run, retention sweep, DR drill
      agent.py                   # /harvest-agent (ADV-gated) + tlh/* primitives
      auth.py                    # /api/token (TOTP), /api/me, verify-email, MFA enroll/verify
      backtest.py                # POST backtest (with hypothetical-performance disclosures)
      billing.py                 # Stripe checkout / portal / webhook / status / invoices
      clients.py                 # Advisor-managed clients
      compliance.py              # Exam export
      corporate_actions.py       # Split / delist / spinoff / merger-cash / ticker-change
      cpa_invites.py             # Invite-your-CPA magic-link flow
      esg.py                     # Exclusions
      households.py              # Wash-sale scope
      import_lots.py             # CSV import preview + commit
      market.py                  # Quotes passthrough
      portfolios.py              # CRUD + tax-lot detail + 8949 + tax report PDF
      positions.py               # Spec-ID sell
      rebalancing.py             # Drift + execute
      signup.py                  # /signup/individual
      tax_loss.py                # Opportunities + harvest
      trade_plans.py             # Lifecycle + approve + reconcile with diff
      users.py                   # Admin user mgmt
  services/
    ai_guardrails.py             # Hard guardrails + schema validation
    audit.py                     # log_audit + log_recommendation
    backtest_service.py          # yfinance-backed backtest
    backup_service.py            # Nightly pg_dump → S3
    billing_service.py           # Stripe state machine
    constituent_store.py         # Persistence for index constituents
    constituents.py              # Wikipedia scrape + market-cap cache
    corporate_action_service.py  # Split / delist / spinoff / merger-cash
    cpa_invite_service.py        # Magic-link issuance + redemption
    csv_importer.py              # Broker CSV parser
    disclosures.py               # Rule 206(4)-1 marketing disclosures
    dividend_service.py          # Finnhub dividends → Transaction
    dr_drill.py                  # Quarterly DR drill runner
    email_service.py             # SMTP / capture hook / verification tokens
    encryption.py                # Fernet EncryptedText TypeDecorator
    esg_service.py
    finnhub_client.py
    household_wash_sale.py
    idempotency.py
    kill_switch.py
    lot_engine.py                # Core tax-lot engine
    metrics_service.py           # Admin dashboard aggregator
    notifications.py             # Harvest-opportunity email
    portfolio_service.py
    reasoning_builder.py         # Citations + confidence + caveats
    rebalancing_service.py
    reconcile_diff.py            # Post-trade fill diff
    retention.py                 # 7-year archive rotation
    secrets.py                   # AWS Secrets Manager resolver
    sell_service.py              # Manual spec-ID sell
    tax_loss_service.py
    tax_pdf.py                   # reportlab Form 8949 PDF
    ticker_change_service.py     # Symbol rename automation
    tlh_agent.py                 # Claude tool-use loop + demo mode
    tlh_tools.py                 # 5 structured primitives
    totp_service.py              # pyotp wrapper
    trade_export.py              # Schwab / Fidelity / generic CSV exports
    trade_plan_service.py        # Lifecycle + idempotency
    user_service.py
  mcp_server.py                  # Standalone MCP server
  scripts/
    loadtest.py                  # Locust profile for 500 concurrent users
    restore_from_backup.py       # DR restore CLI
    seed_demo.py                 # demo@example.com fixture
  tests/                         # Full suite — 100+ passing
alembic/
  env.py
  versions/
    001_initial_schema.py
    002_m1_through_m6.py
    003_security_hardening.py
    004_subscriptions.py
    005_admin_mfa.py
    006_cpa_invites.py
frontend/
  index.html                     # Single-page app — login, onboarding, dashboard, admin, billing, metrics
Dockerfile                       # Runs alembic upgrade head, non-root, postgresql-client installed
docker-compose.yml               # postgres + api + caddy
Caddyfile
fly.toml                         # Fly.io blueprint
render.yaml                      # Render blueprint
docs/
  TODO.md
  deployment.md
  dr_runbook.md
  launch_strategy.md
  pricing_strategy.md
  regulatory_scoping_memo.md
  loadtest.md
.env                             # Secrets (not committed)
pytest.ini
```
