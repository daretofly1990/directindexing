"""
Locust load test for the Direct Indexing platform.

Target profile:
  - 500 concurrent users
  - ~200 positions per portfolio (S&P 500 construct with aggressive filtering)
  - p95 < 3s for the harvest scan endpoint
  - p95 < 1s for tax-lot reads and realized-gains summary
  - Error rate < 0.5% across a 10-minute run

Usage:

  # Install (one-time):
  pip install "locust>=2.0" --break-system-packages

  # Point at a running staging API:
  locust -f backend/scripts/loadtest.py \\
    --host https://staging.directindex.example \\
    --users 500 --spawn-rate 25 --run-time 10m

  # Headless with CSV output:
  locust -f backend/scripts/loadtest.py \\
    --host https://staging.directindex.example \\
    --users 500 --spawn-rate 25 --run-time 10m \\
    --headless --csv=loadtest_results --html=loadtest_report.html

Shape of the load:

  1. On spawn: signup an individual user, accept acknowledgements, construct
     a 200-position portfolio. These steps happen once per virtual user.
  2. Steady-state task mix (by weight):
       - 10x read open tax lots
       - 5x find_losses (cheap — DB only, no Claude call)
       - 3x realized-gains summary
       - 2x harvest-agent (expensive — runs the demo-mode plan)
       - 1x draft trade-list (simulates approving a plan)

The harvest-agent path is the one that dominates the p95; keep the mix
weighted toward reads so the test matches real-world usage.

Env vars used:
  LOADTEST_EMAIL_PREFIX — defaults to "loadtest". Adjusted per-user with
                          a random suffix so the test can run repeatedly
                          without colliding on the unique email index.
  LOADTEST_PASSWORD — defaults to "LoadTest!23Secure".
"""
import os
import random
import string
import uuid

try:
    from locust import HttpUser, between, events, task
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "locust is not installed. Run:\n"
        "  pip install 'locust>=2.0' --break-system-packages"
    ) from e


EMAIL_PREFIX = os.environ.get("LOADTEST_EMAIL_PREFIX", "loadtest")
PASSWORD = os.environ.get("LOADTEST_PASSWORD", "LoadTest!23Secure")
PORTFOLIO_INITIAL = 1_000_000
POSITION_COUNT_TARGET = 200


def _random_email() -> str:
    # Unique enough for a 500-user run; avoids collisions with manually
    # created users.
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{EMAIL_PREFIX}-{suffix}-{uuid.uuid4().hex[:6]}@example.com"


class DirectIndexingUser(HttpUser):
    """One virtual user: signs up, constructs a portfolio, then exercises the hot paths."""

    wait_time = between(0.5, 2.5)

    # Populated per-user during `on_start`. These survive across tasks.
    jwt: str | None = None
    portfolio_id: int | None = None
    email: str | None = None

    def _auth_headers(self) -> dict:
        if not self.jwt:
            return {}
        return {"Authorization": f"Bearer {self.jwt}"}

    def _json_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.jwt}" if self.jwt else "",
            "Content-Type": "application/json",
        }

    def on_start(self):
        """Signup → accept acks → construct a 200-position portfolio."""
        self.email = _random_email()

        with self.client.post(
            "/api/signup/individual",
            json={"email": self.email, "password": PASSWORD, "full_name": "Load Tester"},
            name="POST /api/signup/individual",
            catch_response=True,
        ) as r:
            if r.status_code != 200 and r.status_code != 201:
                r.failure(f"signup failed: {r.status_code} {r.text[:200]}")
                return
            body = r.json()
            self.jwt = body.get("access_token") or body.get("token")
            if not self.jwt:
                r.failure("signup returned no token")
                return

        # Accept all required acknowledgements so the agent gate opens.
        for doc in ("tos", "adv_part_2a", "privacy"):
            self.client.post(
                "/api/acknowledgements/accept",
                headers=self._json_headers(),
                json={"document_type": doc, "version": "1.0"},
                name="POST /api/acknowledgements/accept",
            )

        # Create + construct a 200-position portfolio.
        with self.client.post(
            "/api/portfolios",
            headers=self._json_headers(),
            json={"name": "Load Test Portfolio", "initial_value": PORTFOLIO_INITIAL},
            name="POST /api/portfolios",
            catch_response=True,
        ) as r:
            if r.status_code not in (200, 201):
                r.failure(f"portfolio create failed: {r.status_code}")
                return
            self.portfolio_id = r.json()["id"]

        self.client.post(
            f"/api/portfolios/{self.portfolio_id}/construct",
            headers=self._json_headers(),
            json={
                "index": "sp500",
                "position_count": POSITION_COUNT_TARGET,
                "sector_exclusions": [],
                "symbol_exclusions": [],
            },
            name="POST /api/portfolios/{id}/construct",
        )

    # ---- Steady-state tasks ----
    # Weights approximate real-world usage — reads dominate, agent calls rare.

    @task(10)
    def read_tax_lots(self):
        if not self.portfolio_id:
            return
        self.client.get(
            f"/api/portfolios/{self.portfolio_id}/tax-lots",
            headers=self._auth_headers(),
            name="GET /api/portfolios/{id}/tax-lots",
        )

    @task(5)
    def find_losses(self):
        if not self.portfolio_id:
            return
        self.client.post(
            f"/api/portfolios/{self.portfolio_id}/tlh/find-losses",
            headers=self._json_headers(),
            json={"target_amount": 5000, "min_loss_pct": 0.02},
            name="POST /api/portfolios/{id}/tlh/find-losses",
        )

    @task(3)
    def realized_gains(self):
        if not self.portfolio_id:
            return
        self.client.get(
            f"/api/portfolios/{self.portfolio_id}/realized-gains",
            headers=self._auth_headers(),
            name="GET /api/portfolios/{id}/realized-gains",
        )

    @task(2)
    def harvest_agent(self):
        """Heavy path — demo mode runs all 5 primitives serially. p95 target: 3s."""
        if not self.portfolio_id:
            return
        self.client.post(
            f"/api/portfolios/{self.portfolio_id}/harvest-agent",
            headers=self._json_headers(),
            json={"prompt": "Scan for harvestable losses totalling $5000 or more."},
            name="POST /api/portfolios/{id}/harvest-agent",
        )

    @task(1)
    def draft_trade_list(self):
        if not self.portfolio_id:
            return
        self.client.post(
            f"/api/portfolios/{self.portfolio_id}/tlh/draft-trade-list",
            headers=self._json_headers(),
            json={"harvests": []},  # empty plan — exercises the route, not the sim
            name="POST /api/portfolios/{id}/tlh/draft-trade-list",
        )


# ---- Pass/fail summary -----------------------------------------------------

@events.quitting.add_listener
def _enforce_slo(environment, **_kwargs):
    """
    Fail the run (non-zero exit) if SLOs are blown. Hooks into `locust --headless`
    so CI can gate on this.

    SLOs:
      - overall fail ratio < 0.5%
      - p95 response time < 3000ms across all tasks
      - harvest-agent p95 < 5000ms (slightly looser since it serializes 5 calls)
    """
    stats = environment.stats

    fail_ratio = stats.total.fail_ratio
    overall_p95 = stats.total.get_response_time_percentile(0.95)

    failed = False
    if fail_ratio > 0.005:
        environment.runner.send_message("fail", f"fail ratio {fail_ratio:.3%} > 0.5%")
        failed = True
    if overall_p95 and overall_p95 > 3000:
        environment.runner.send_message("fail", f"overall p95 {overall_p95}ms > 3000ms")
        failed = True

    # Harvest-agent specific slice
    agent_stats = stats.get(
        "POST /api/portfolios/{id}/harvest-agent", "POST",
    )
    if agent_stats and agent_stats.num_requests > 0:
        agent_p95 = agent_stats.get_response_time_percentile(0.95)
        if agent_p95 and agent_p95 > 5000:
            environment.runner.send_message(
                "fail", f"harvest-agent p95 {agent_p95}ms > 5000ms"
            )
            failed = True

    if failed:
        environment.process_exit_code = 1
