# Deployment runbook

Two supported targets: **Fly.io** (simpler, single command) and **Render.com** (more managed UI). Pick one. Both run the same Dockerfile.

## Before you deploy (one-time)

Collect the credentials you'll need. Having these ready makes the deploy a 20-minute task; missing any will stall you.

| Env var | Source |
|---|---|
| `FINNHUB_API_KEY` | https://finnhub.io/ — free tier is fine |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ — optional (demo mode works without) |
| `JWT_SECRET` | `openssl rand -hex 32` |
| `ADMIN_EMAIL` | your email |
| `ADMIN_PASSWORD` | strong passphrase |
| `FIELD_ENCRYPTION_KEYS` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — **back this up**, losing it = losing encrypted data |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | Mailtrap sandbox creds for staging; swap for Mailtrap live, SendGrid, Postmark, or SES in prod |
| `SMTP_FROM` | the verified sender address from your provider |
| `APP_BASE_URL` | the URL users will actually hit (used in verification email links) |
| `CORS_ORIGINS` | same as APP_BASE_URL, comma-separated if multiple |
| `SENTRY_DSN` | optional; https://sentry.io/ project → Settings → Client Keys |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` / six price IDs | optional; required only if you turn on billing |
| `S3_BACKUP_BUCKET` / AWS creds | optional; required for nightly DB backups |

## Fly.io

```
fly auth login
fly apps create direct-indexing                     # pick your own app name
fly postgres create --name di-db --region iad       # managed Postgres
fly postgres attach di-db                           # injects DATABASE_URL
fly secrets set \
    JWT_SECRET=$(openssl rand -hex 32) \
    ADMIN_EMAIL=you@example.com \
    ADMIN_PASSWORD=... \
    FINNHUB_API_KEY=... \
    FIELD_ENCRYPTION_KEYS=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
    SMTP_HOST=sandbox.smtp.mailtrap.io SMTP_PORT=2525 SMTP_USER=... SMTP_PASSWORD=...
fly deploy
```

Verify:

```
fly open                      # opens the deployed URL
fly ssh console -C "curl -sf localhost:8000/health"
fly logs                      # stream structured JSON logs
```

## Render.com

1. Push this repo to GitHub.
2. Render dashboard → **New → Blueprint** → select the repo.
3. Render parses `render.yaml`, provisions the web service + Postgres, wires `DATABASE_URL` automatically.
4. In the dashboard, set every env var marked `sync: false` (the secrets list above).
5. Hit **Apply** — Render builds, runs `alembic upgrade head`, starts the app.

Verify by visiting `https://<your-app>.onrender.com/health`.

## First-time smoke test (either platform)

```
# 1. Basic liveness
curl -sf https://<your-app>/health
# → {"status":"ok"}

# 2. Deep health (DB + Finnhub + Anthropic + encryption)
curl -sf https://<your-app>/api/healthz | jq
# → {"status":"ok","dependencies":{...}}

# 3. Log in as admin
curl -sf https://<your-app>/api/auth/token \
    -d "username=$ADMIN_EMAIL&password=$ADMIN_PASSWORD" | jq -r '.access_token'

# 4. Send a test email (confirms SMTP is wired)
curl -sf -X POST https://<your-app>/api/admin/email/test \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"to":"you@example.com"}'

# 5. Seed a demo portfolio so the UI isn't empty on first look
fly ssh console -C "python -m backend.scripts.seed_demo"
# or on Render: dashboard → Shell → python -m backend.scripts.seed_demo
```

## Post-deploy checklist

- [ ] `/health` returns 200
- [ ] `/api/healthz` shows `database: ok` and `finnhub: ok`
- [ ] Admin can log in
- [ ] Test email lands in Mailtrap (sandbox mode) or real inbox (live mode)
- [ ] Enroll TOTP for every admin account (`POST /api/auth/mfa/enroll` — UI coming)
- [ ] Set `S3_BACKUP_BUCKET` and confirm first nightly backup runs (log line at 02:00 UTC: "Nightly backup: status=ok")
- [ ] Set `SENTRY_DSN` and trigger a test error to confirm it lands in Sentry
- [ ] Configure a custom domain + TLS (Fly: `fly certs add yourdomain.com`; Render: dashboard → Custom Domains)
- [ ] Schedule the quarterly DR drill — see `docs/dr_runbook.md`

## Gotchas this deployment has surfaced before

- **`pg_dump` not on PATH in a bare Python image** — the Dockerfile now installs `postgresql-client`. If you use a different base image, install it.
- **`FIELD_ENCRYPTION_KEYS` unset in prod** — the app still boots, but new rows are written as plaintext and a warning is logged. Set the key *before* the first real write, or you'll have a mixed-state DB that's painful to clean up.
- **`APP_BASE_URL` mismatch** — if it doesn't match the actual domain, verification links in emails point at the wrong host.
- **`CORS_ORIGINS` wildcard in prod** — the middleware logs a warning. Tighten to your known origins.
- **Alembic migration conflicts** — if you deploy while a migration is in-flight from another instance, you'll get locks. On Fly, scale to 1 machine during migrations; on Render, only one instance runs `alembic upgrade head` at a time via the release command pattern.
