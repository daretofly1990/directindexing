"""
Tests for backend.services.constituents.

Runs entirely offline against a saved Wikipedia fixture
(backend/tests/fixtures/sp500_wiki.html). Any live network call here is a
regression — the test monkey-patches _fetch_url to guarantee that.
"""

import asyncio
import os
from pathlib import Path

import pytest

from backend.services import constituents as C


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sp500_wiki.html"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture() -> str:
    assert FIXTURE_PATH.exists(), (
        f"Missing fixture {FIXTURE_PATH}. Run backend/tests/fixtures/create_fixture.py "
        "once in a network-enabled environment to save it."
    )
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _forbid_network(monkeypatch):
    """Raise if anything tries to make a real HTTP call during a test."""

    async def _blocked(url):  # noqa: ANN001 — match signature
        raise AssertionError(f"Network access is forbidden in tests; attempted: {url}")

    monkeypatch.setattr(C, "_fetch_url", _blocked)


# ---------------------------------------------------------------------------
# Parser tests — S&P 500
# ---------------------------------------------------------------------------


def test_parse_sp500_fixture_row_count():
    html = _load_fixture()
    rows = C.parse_sp500_html(html)
    # The saved fixture should yield a plausible S&P 500 list. We allow a
    # loose range since Wikipedia layout can add/remove a handful of rows
    # between snapshots. Production target is ~500; fixture minimum is 400.
    assert 400 <= len(rows) <= 510, f"Unexpected row count: {len(rows)}"


def test_parse_sp500_required_fields_present():
    html = _load_fixture()
    rows = C.parse_sp500_html(html)
    required = {"symbol", "name", "sector", "industry", "weight"}
    for row in rows:
        assert required.issubset(row.keys()), f"Missing fields in {row}"
        assert row["symbol"], f"Empty symbol in row: {row}"
        assert row["name"], f"Empty name in row: {row}"
        assert row["sector"], f"Empty sector in row: {row}"


def test_parse_sp500_symbols_are_upper_alphanumeric():
    html = _load_fixture()
    rows = C.parse_sp500_html(html)
    import re

    pattern = re.compile(r"^[A-Z0-9.\-]+$")
    for row in rows:
        assert pattern.match(row["symbol"]), f"Bad symbol format: {row['symbol']}"


def test_parse_sp500_unique_symbols():
    html = _load_fixture()
    rows = C.parse_sp500_html(html)
    symbols = [r["symbol"] for r in rows]
    # Wikipedia sometimes has two share classes (e.g. GOOG + GOOGL) so we
    # allow up to 3 duplicates; a massive duplicate count would indicate a
    # parser bug.
    dupes = len(symbols) - len(set(symbols))
    assert dupes <= 3, f"Too many duplicate symbols ({dupes}): {symbols}"


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------


def test_display_to_yf_converts_dots_to_dashes():
    assert C.display_to_yf("BRK.B") == "BRK-B"
    assert C.display_to_yf("BF.B") == "BF-B"
    # Idempotent on plain tickers
    assert C.display_to_yf("AAPL") == "AAPL"


def test_yf_to_display_converts_dashes_to_dots():
    assert C.yf_to_display("BRK-B") == "BRK.B"
    assert C.yf_to_display("BF-B") == "BF.B"
    assert C.yf_to_display("AAPL") == "AAPL"


def test_normalize_symbol_backward_compat_alias():
    # normalize_symbol is the backward-compatible alias = yf_to_display
    assert C.normalize_symbol("BRK-B") == "BRK.B"


def test_round_trip_ticker_normalisation():
    for t in ("AAPL", "MSFT", "BRK.B", "BF.B"):
        assert C.yf_to_display(C.display_to_yf(t)) == t


# ---------------------------------------------------------------------------
# Weight computation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_weights_normalises_to_one(monkeypatch):
    """Injecting known market caps must produce weights summing to 1.0."""

    def fake_caps(tickers):
        # Deterministic mock caps
        return {t: float(i + 1) * 1e9 for i, t in enumerate(tickers)}

    monkeypatch.setattr(C, "_fetch_market_caps_sync", fake_caps)
    _forbid_network(monkeypatch)

    raw = [
        {"symbol": "AAA", "name": "A", "sector": "Tech", "industry": "", "weight": 0.0},
        {"symbol": "BBB", "name": "B", "sector": "Tech", "industry": "", "weight": 0.0},
        {"symbol": "CCC", "name": "C", "sector": "Health", "industry": "", "weight": 0.0},
    ]
    out = await C.compute_weights(raw)
    total = sum(r["weight"] for r in out)
    assert abs(total - 1.0) < 1e-6, f"weights sum = {total}"
    # Largest cap should have largest weight
    assert out[2]["weight"] > out[0]["weight"]


@pytest.mark.asyncio
async def test_compute_weights_missing_caps_fallback_equal(monkeypatch):
    """If all caps are None, fall back to equal weighting."""

    def fake_caps(tickers):
        return {t: None for t in tickers}

    monkeypatch.setattr(C, "_fetch_market_caps_sync", fake_caps)
    _forbid_network(monkeypatch)

    raw = [
        {"symbol": "AAA", "name": "A", "sector": "", "industry": "", "weight": 0.0},
        {"symbol": "BBB", "name": "B", "sector": "", "industry": "", "weight": 0.0},
        {"symbol": "CCC", "name": "C", "sector": "", "industry": "", "weight": 0.0},
        {"symbol": "DDD", "name": "D", "sector": "", "industry": "", "weight": 0.0},
    ]
    out = await C.compute_weights(raw)
    for row in out:
        assert abs(row["weight"] - 0.25) < 1e-9


@pytest.mark.asyncio
async def test_compute_weights_partial_caps(monkeypatch):
    """Missing individual caps get weight 0, others sum to 1."""

    def fake_caps(tickers):
        caps = {}
        for i, t in enumerate(tickers):
            caps[t] = (i + 1) * 1e9 if i % 2 == 0 else None
        return caps

    monkeypatch.setattr(C, "_fetch_market_caps_sync", fake_caps)
    _forbid_network(monkeypatch)

    raw = [
        {"symbol": f"T{i}", "name": f"T{i}", "sector": "", "industry": "", "weight": 0.0}
        for i in range(4)
    ]
    out = await C.compute_weights(raw)
    total = sum(r["weight"] for r in out)
    assert abs(total - 1.0) < 1e-6
    # Odd-indexed should have weight 0
    for i, row in enumerate(out):
        if i % 2 == 1:
            assert row["weight"] == 0.0


# ---------------------------------------------------------------------------
# Network guard — ensure fetcher raises without network in unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_sp500_uses_fetch_url(monkeypatch):
    """fetch_sp500 should call _fetch_url, not reach the real internet in tests."""
    html = _load_fixture()

    async def fake_fetch(url):
        assert "S%26P_500" in url or "S&P_500" in url
        return html

    monkeypatch.setattr(C, "_fetch_url", fake_fetch)
    rows = await C.fetch_sp500()
    assert len(rows) > 400
    assert rows[0]["symbol"]
