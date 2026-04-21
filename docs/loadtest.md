# Load testing

Target profile for Phase 1 launch:

| Metric | Target |
| --- | --- |
| Concurrent users | 500 |
| Positions per portfolio | ~200 (S&P 500 construct) |
| Harvest-agent endpoint p95 | < 3s (strict) |
| Harvest-agent endpoint p95 (demo mode, 5 primitives serial) | < 5s (loose) |
| Tax-lot read + realized-gains p95 | < 1s |
| Error rate | < 0.5% across a 10-minute run |

The script is [`backend/scripts/loadtest.py`](../backend/scripts/loadtest.py). It's a [Locust](https://locust.io) profile — one virtual user signs up, accepts the three acknowledgements (ToS / ADV 2A / Privacy), constructs a 200-position portfolio, then exercises the hot paths in a read-heavy mix.

## Task mix

The steady-state task mix is weighted toward reads because that's what the real product looks like — a user checks their dashboard many times for each harvest they actually run.

| Task | Weight | Why |
| --- | --- | --- |
| `GET /api/portfolios/{id}/tax-lots` | 10 | Dashboard read on every tab switch |
| `POST .../tlh/find-losses` | 5 | Cheap (DB only, no agent call) — run often |
| `GET .../realized-gains` | 3 | Tax view; a few times per session |
| `POST .../harvest-agent` | 2 | The heavy path — 5 primitives serially in demo mode |
| `POST .../tlh/draft-trade-list` | 1 | Simulates the approve-plan flow |

The harvest-agent path is the one that drives the overall p95. Keep the weighting read-heavy (15 reads per 2 agent calls) so the test matches real-world latency distribution rather than artificially concentrating on the expensive path.

## Setup

One-time:

```bash
pip install 'locust>=2.0' --break-system-packages
```

## Running

Interactive (browser UI at http://localhost:8089):

```bash
locust -f backend/scripts/loadtest.py \
  --host https://staging.directindex.example \
  --users 500 --spawn-rate 25 --run-time 10m
```

Headless (for CI):

```bash
locust -f backend/scripts/loadtest.py \
  --host https://staging.directindex.example \
  --users 500 --spawn-rate 25 --run-time 10m \
  --headless \
  --csv=loadtest_results \
  --html=loadtest_report.html
```

Spawn rate of 25 users/sec means the full 500 ramp up over 20 seconds, leaving ~9.5 minutes of steady-state traffic.

## Environment variables

The script is stateless across runs — every virtual user signs up a fresh account with a random email suffix. Two knobs control this:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOADTEST_EMAIL_PREFIX` | `loadtest` | Email prefix for synthetic users. Pick something distinct per environment so cleanup is easy. |
| `LOADTEST_PASSWORD` | `LoadTest!23Secure` | Satisfies the password-complexity rule. |

Since every run creates new users, the staging DB grows. Use `DELETE FROM users WHERE email LIKE 'loadtest-%';` (cascades via FK) to clean up after a run, or spin the scratch DB from a backup before each run.

## SLO enforcement

The script hooks `@events.quitting` and fails the Locust run (non-zero exit) if:

- Overall fail ratio exceeds 0.5%
- Overall p95 response time exceeds 3000ms
- `POST /api/portfolios/{id}/harvest-agent` p95 exceeds 5000ms

This makes the load test CI-gateable — wire it into a staging-deploy workflow and let a regression block the promotion to prod.

## What it does NOT cover

- **Real agent calls.** The harvest-agent endpoint runs in demo mode unless `ANTHROPIC_API_KEY` is set in the staging environment. If you want to load-test the live agent, set the key AND expect higher p95 — Anthropic's Haiku 4.5 latency dominates that path.
- **Cold cache.** After the first few users, the constituent cache and market-cap cache are warm. Real cold-start latency (first signup of the day) is not captured here — exercise that separately by hitting `/api/portfolios/{id}/construct` on a fresh container.
- **Persistent sessions.** Every virtual user creates a new signup. Real traffic has a mix of new vs. returning sessions; the read-heavy task mix approximates this but doesn't model session length.
- **Billing paths.** Stripe endpoints are skipped — they go through Stripe's infra, not ours.

## Pre-flight checklist

Before running against staging:

1. Scale staging web workers to production-equivalent count (gunicorn `-w` or Fly/Render instance count)
2. Attach a staging-sized Postgres (same CPU/memory as prod plan)
3. Confirm `DEMO_MODE=1` OR `ANTHROPIC_API_KEY` is set — otherwise the agent route 503s
4. Have `tmux` / `screen` ready on staging so you can watch DB connections (`SELECT count(*) FROM pg_stat_activity;`) and worker logs during the run
5. Capture `/api/admin/metrics` before and after — useful sanity check that the test actually exercised what you think it did

## Interpreting results

Locust's CSV emits one row per endpoint per second. The three files that matter:

- `loadtest_results_stats.csv` — per-endpoint p50/p95/p99, RPS, failure count
- `loadtest_results_failures.csv` — every failed request with exception message
- `loadtest_report.html` — Locust's rendered summary, easiest for a spot check

For a passing run at target load you should see:

- Harvest-agent p95 in the 2000–3500ms range (demo mode). Above 4000ms means the serialized primitives are queuing somewhere — check DB pool size first.
- Tax-lot reads p95 < 500ms. Above 1000ms means missing index or ORM N+1.
- Failures concentrated in `POST /api/signup/individual` only if SlowAPI is rate-limiting the spawn burst — that's expected if spawn-rate exceeds 20/min at the IP level. Lower `--spawn-rate` or whitelist the load-test source IP.

## Next steps after a clean run

1. Record the pass in the deploy runbook with date + commit SHA
2. If any SLO was tight (e.g. agent p95 at 4800ms), open a perf item — the budget is 5000ms, less than 200ms of headroom isn't enough to absorb a bad day
3. Re-run after any material change to the agent primitives or the harvest scan query path
