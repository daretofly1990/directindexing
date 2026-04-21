"""
Constituent fetcher for S&P 500, NASDAQ-100, and Russell 1000 indexes.

Sources:
  - S&P 500:   Wikipedia  https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
  - NASDAQ-100: Wikipedia https://en.wikipedia.org/wiki/Nasdaq-100
  - Russell 1000: Wikipedia https://en.wikipedia.org/wiki/Russell_1000_Index
                  Fallback: iShares IWB holdings CSV

All fetchers return a list of dicts with keys:
  symbol, name, sector, industry, weight (raw float, not yet normalised)

`compute_weights` normalises to sum-to-1.0 using yfinance market cap data.
"""

import asyncio
import logging
import re
import urllib.request
import urllib.error
from typing import Optional

from bs4 import BeautifulSoup

# httpx is listed in requirements.txt; import lazily so the module
# can be imported even when httpx is not yet installed (e.g. in test
# environments that don't need network fetching).
try:
    import httpx as _httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticker normalisation helpers
# ---------------------------------------------------------------------------

def display_to_yf(symbol: str) -> str:
    """Convert display ticker (BRK.B) to yfinance form (BRK-B)."""
    return symbol.replace(".", "-")


def yf_to_display(symbol: str) -> str:
    """Convert yfinance ticker (BRK-B) to display form (BRK.B)."""
    return symbol.replace("-", ".")

# Backward-compatible aliases used by tests and other modules
normalize_symbol = yf_to_display
yf_symbol = display_to_yf


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
_TIMEOUT = 30.0
_MAX_RETRIES = 3


async def _fetch_url(url: str) -> str:
    """Fetch a URL with retries and exponential backoff.

    Uses httpx when available; falls back to stdlib urllib.request.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            if _HTTPX_AVAILABLE:
                timeout_obj = _httpx.Timeout(_TIMEOUT)
                async with _httpx.AsyncClient(
                    headers=_HEADERS, timeout=timeout_obj, follow_redirects=True
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.text
            else:
                # Sync fallback via urllib (runs in executor to avoid blocking)
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, _urllib_get, url)
        except Exception as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning(
                "Fetch attempt %d/%d failed for %s: %s. Retrying in %ds.",
                attempt + 1, _MAX_RETRIES, url, exc, wait,
            )
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(wait)

    raise RuntimeError(f"Failed to fetch {url} after {_MAX_RETRIES} attempts") from last_exc


def _urllib_get(url: str) -> str:
    """Synchronous URL fetch via urllib.request."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# S&P 500
# ---------------------------------------------------------------------------

async def fetch_sp500() -> list[dict]:
    """
    Scrape the S&P 500 constituent list from Wikipedia.
    Returns list of dicts: symbol, name, sector, industry.
    Expects ~500 rows.
    """
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = await _fetch_url(url)
    return parse_sp500_html(html)


def parse_sp500_html(html: str) -> list[dict]:
    """Parse S&P 500 Wikipedia HTML. Separated for unit testing."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", {"id": "constituents"})
    if table is None:
        # Fallback: first wikitable with a Symbol column
        for tbl in soup.find_all("table", class_="wikitable"):
            headers = [th.get_text(strip=True) for th in tbl.find_all("th")]
            if any("Symbol" in h for h in headers):
                table = tbl
                break
    if table is None:
        raise RuntimeError("Could not locate S&P 500 constituents table on Wikipedia page.")

    rows = table.find_all("tr")
    headers = [th.get_text(strip=True) for th in rows[0].find_all("th")]

    # Column index discovery
    def _col(candidates: list[str]) -> int:
        for c in candidates:
            for i, h in enumerate(headers):
                if c.lower() in h.lower():
                    return i
        return -1

    idx_symbol = _col(["Symbol", "Ticker"])
    idx_name = _col(["Security", "Company", "Name"])
    idx_sector = _col(["GICS Sector", "Sector"])
    idx_industry = _col(["GICS Sub-Industry", "Sub-Industry", "Industry"])

    if idx_symbol == -1:
        raise RuntimeError(f"Symbol column not found. Headers: {headers}")

    constituents = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) <= max(idx_symbol, idx_name if idx_name != -1 else 0):
            continue

        symbol = cells[idx_symbol].get_text(strip=True)
        symbol = re.sub(r"[^A-Z0-9.\-]", "", symbol.upper())
        if not symbol:
            continue

        name = cells[idx_name].get_text(strip=True) if idx_name != -1 and idx_name < len(cells) else ""
        sector = cells[idx_sector].get_text(strip=True) if idx_sector != -1 and idx_sector < len(cells) else ""
        industry = cells[idx_industry].get_text(strip=True) if idx_industry != -1 and idx_industry < len(cells) else ""

        constituents.append({
            "symbol": symbol,
            "name": name,
            "sector": sector,
            "industry": industry,
            "weight": 0.0,
        })

    logger.info("Fetched %d S&P 500 constituents from Wikipedia.", len(constituents))
    return constituents


# ---------------------------------------------------------------------------
# NASDAQ-100
# ---------------------------------------------------------------------------

async def fetch_nasdaq100() -> list[dict]:
    """
    Scrape the NASDAQ-100 constituent list from Wikipedia.
    Returns ~100 rows.
    """
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    html = await _fetch_url(url)
    return parse_nasdaq100_html(html)


def parse_nasdaq100_html(html: str) -> list[dict]:
    """Parse NASDAQ-100 Wikipedia HTML. Separated for unit testing."""
    soup = BeautifulSoup(html, "lxml")

    # Look for a table with Ticker / Symbol header
    table = None
    for tbl in soup.find_all("table", class_="wikitable"):
        headers_text = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
        if any("ticker" in h or "symbol" in h for h in headers_text):
            table = tbl
            break

    if table is None:
        raise RuntimeError("Could not locate NASDAQ-100 constituents table on Wikipedia page.")

    rows = table.find_all("tr")
    headers = [th.get_text(strip=True) for th in rows[0].find_all("th")]

    def _col(candidates: list[str]) -> int:
        for c in candidates:
            for i, h in enumerate(headers):
                if c.lower() in h.lower():
                    return i
        return -1

    idx_symbol = _col(["Ticker", "Symbol"])
    idx_name = _col(["Company", "Security", "Name"])
    idx_sector = _col(["GICS Sector", "Sector"])
    idx_industry = _col(["GICS Sub-Industry", "Sub-Industry", "Industry"])

    if idx_symbol == -1:
        raise RuntimeError(f"Ticker/Symbol column not found. Headers: {headers}")

    constituents = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells or len(cells) <= idx_symbol:
            continue
        symbol = cells[idx_symbol].get_text(strip=True)
        symbol = re.sub(r"[^A-Z0-9.\-]", "", symbol.upper())
        if not symbol:
            continue
        name = cells[idx_name].get_text(strip=True) if idx_name != -1 and idx_name < len(cells) else ""
        sector = cells[idx_sector].get_text(strip=True) if idx_sector != -1 and idx_sector < len(cells) else ""
        industry = cells[idx_industry].get_text(strip=True) if idx_industry != -1 and idx_industry < len(cells) else ""

        constituents.append({
            "symbol": symbol,
            "name": name,
            "sector": sector,
            "industry": industry,
            "weight": 0.0,
        })

    logger.info("Fetched %d NASDAQ-100 constituents from Wikipedia.", len(constituents))
    return constituents


# ---------------------------------------------------------------------------
# Russell 1000
# ---------------------------------------------------------------------------

_ISHARES_IWB_CSV = (
    "https://www.ishares.com/us/products/239707/"
    "ishares-russell-1000-etf/1467271812596.ajax"
    "?fileType=csv&fileName=IWB_holdings&dataType=fund"
)


async def fetch_russell1000() -> list[dict]:
    """
    Scrape the Russell 1000 constituent list from Wikipedia.
    Falls back to iShares IWB holdings CSV if <900 rows.
    """
    url = "https://en.wikipedia.org/wiki/Russell_1000_Index"
    try:
        html = await _fetch_url(url)
        constituents = parse_russell1000_html(html)
    except Exception as exc:
        logger.warning("Russell 1000 Wikipedia fetch failed: %s. Trying iShares CSV.", exc)
        constituents = []

    if len(constituents) < 900:
        logger.info(
            "Russell 1000 Wikipedia gave %d rows (<900), trying iShares IWB CSV.",
            len(constituents),
        )
        try:
            constituents = await _fetch_russell1000_ishares()
        except Exception as exc2:
            logger.error("iShares IWB fallback also failed: %s", exc2)
            if not constituents:
                raise RuntimeError(
                    "Could not fetch Russell 1000 from Wikipedia or iShares."
                ) from exc2

    logger.info("Final Russell 1000 count: %d", len(constituents))
    return constituents


def parse_russell1000_html(html: str) -> list[dict]:
    """Parse Russell 1000 Wikipedia HTML. Returns what it can find."""
    soup = BeautifulSoup(html, "lxml")
    constituents = []

    for tbl in soup.find_all("table", class_="wikitable"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        headers = [th.get_text(strip=True) for th in rows[0].find_all("th")]
        headers_lower = [h.lower() for h in headers]

        # Need at least a ticker/symbol column
        sym_idx = -1
        for candidate in ("ticker", "symbol"):
            for i, h in enumerate(headers_lower):
                if candidate in h:
                    sym_idx = i
                    break
            if sym_idx != -1:
                break

        if sym_idx == -1:
            continue

        name_idx = next(
            (i for i, h in enumerate(headers_lower) if "company" in h or "name" in h or "security" in h), -1
        )
        sector_idx = next((i for i, h in enumerate(headers_lower) if "sector" in h), -1)

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells or len(cells) <= sym_idx:
                continue
            symbol = re.sub(r"[^A-Z0-9.\-]", "", cells[sym_idx].get_text(strip=True).upper())
            if not symbol:
                continue
            name = cells[name_idx].get_text(strip=True) if name_idx != -1 and name_idx < len(cells) else ""
            sector = cells[sector_idx].get_text(strip=True) if sector_idx != -1 and sector_idx < len(cells) else ""
            constituents.append({
                "symbol": symbol, "name": name, "sector": sector,
                "industry": "", "weight": 0.0,
            })

        if len(constituents) >= 900:
            break

    return constituents


async def _fetch_russell1000_ishares() -> list[dict]:
    """Download iShares IWB holdings CSV and parse it."""
    import io
    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError("pandas is required for iShares CSV parsing.")

    raw = await _fetch_url(_ISHARES_IWB_CSV)

    # iShares CSVs have a few header/footer lines to skip; find the data block
    lines = raw.splitlines()
    data_start = 0
    for i, line in enumerate(lines):
        if line.startswith("Ticker,") or line.startswith('"Ticker",'):
            data_start = i
            break

    csv_text = "\n".join(lines[data_start:])
    df = pd.read_csv(io.StringIO(csv_text), thousands=",", on_bad_lines="skip")

    # Normalise column names
    df.columns = [c.strip().strip('"').lower() for c in df.columns]

    col_map = {
        "ticker": next((c for c in df.columns if "ticker" in c), None),
        "name": next((c for c in df.columns if "name" in c), None),
        "sector": next((c for c in df.columns if "sector" in c), None),
        "weight": next((c for c in df.columns if "weight" in c), None),
        "market_value": next((c for c in df.columns if "market value" in c or "mktval" in c), None),
    }

    if col_map["ticker"] is None:
        raise RuntimeError(f"No Ticker column found in iShares CSV. Columns: {list(df.columns)}")

    constituents = []
    for _, row_data in df.iterrows():
        symbol = str(row_data.get(col_map["ticker"], "")).strip()
        symbol = re.sub(r"[^A-Z0-9.\-]", "", symbol.upper())
        if not symbol or symbol in ("-", "CASH", ""):
            continue

        name = str(row_data.get(col_map["name"], "")).strip() if col_map["name"] else ""
        sector = str(row_data.get(col_map["sector"], "")).strip() if col_map["sector"] else ""

        raw_weight = row_data.get(col_map["weight"], 0.0) if col_map["weight"] else 0.0
        try:
            weight = float(str(raw_weight).replace("%", "").replace(",", "")) / 100.0
        except (ValueError, TypeError):
            weight = 0.0

        constituents.append({
            "symbol": symbol, "name": name, "sector": sector,
            "industry": "", "weight": weight,
        })

    logger.info("Parsed %d Russell 1000 rows from iShares IWB CSV.", len(constituents))
    return constituents


# ---------------------------------------------------------------------------
# Weight computation via yfinance
# ---------------------------------------------------------------------------

_MCAP_CHUNK = 50

# In-process market-cap cache. Decoupled from constituent refresh so a
# mid-week constituent add/drop doesn't trigger 500 fresh yfinance calls.
# Entries live for _MCAP_CACHE_TTL and are keyed on display symbol.
import time as _time

_MCAP_CACHE_TTL = 24 * 60 * 60   # 24 hours
_mcap_cache: dict[str, tuple[Optional[float], float]] = {}


def _cache_get(symbol: str) -> tuple[bool, Optional[float]]:
    """Return (hit, value). value is None if the entry is cached-as-missing."""
    entry = _mcap_cache.get(symbol)
    if entry is None:
        return False, None
    value, ts = entry
    if _time.monotonic() - ts > _MCAP_CACHE_TTL:
        _mcap_cache.pop(symbol, None)
        return False, None
    return True, value


def _cache_set(symbol: str, value: Optional[float]) -> None:
    _mcap_cache[symbol] = (value, _time.monotonic())


def clear_mcap_cache() -> None:
    """Test helper — force a fresh fetch."""
    _mcap_cache.clear()


def _fetch_market_caps_sync(tickers: list[str]) -> dict[str, Optional[float]]:
    """
    Fetch market caps synchronously using yfinance fast_info.
    Uses fast_info.market_cap (single HTTP call per ticker, no full info payload).
    Processes in chunks of _MCAP_CHUNK to avoid URL-length limits.
    Called from a thread pool executor.
    Returns {symbol: market_cap_or_None}.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not available; weights will be equal.")
        return {t: None for t in tickers}

    caps: dict[str, Optional[float]] = {}

    # First pass: pull cached values, build a list of tickers still to fetch
    to_fetch: list[str] = []
    for t in tickers:
        hit, value = _cache_get(t)
        if hit:
            caps[t] = value
        else:
            to_fetch.append(t)

    if not to_fetch:
        logger.info("Market-cap cache: 100%% hit (%d symbols)", len(tickers))
        return caps

    logger.info(
        "Market-cap cache: %d/%d hit, fetching %d fresh",
        len(tickers) - len(to_fetch), len(tickers), len(to_fetch),
    )

    yf_tickers = [display_to_yf(t) for t in to_fetch]

    for chunk_start in range(0, len(to_fetch), _MCAP_CHUNK):
        chunk_disp = to_fetch[chunk_start: chunk_start + _MCAP_CHUNK]
        chunk_yf = yf_tickers[chunk_start: chunk_start + _MCAP_CHUNK]
        try:
            data = yf.Tickers(" ".join(chunk_yf))
            for disp, yft in zip(chunk_disp, chunk_yf):
                try:
                    mc = data.tickers[yft].fast_info.market_cap
                    value = float(mc) if mc and mc > 0 else None
                except Exception:
                    value = None
                caps[disp] = value
                _cache_set(disp, value)
        except Exception as exc:
            logger.warning("yfinance chunk fetch failed: %s", exc)
            for disp in chunk_disp:
                caps.setdefault(disp, None)
                _cache_set(disp, None)

    return caps


async def compute_weights(constituents: list[dict]) -> list[dict]:
    """
    Compute market-cap weights for the given constituents list.
    Modifies and returns a copy with weight and market_cap populated.
    Missing market cap → weight 0.0.

    Falls back to equal weighting if all caps are missing.
    """
    tickers = [c["symbol"] for c in constituents]

    loop = asyncio.get_event_loop()
    caps = await loop.run_in_executor(None, _fetch_market_caps_sync, tickers)

    total_cap = sum(v for v in caps.values() if v is not None and v > 0)

    result = []
    for c in constituents:
        mc = caps.get(c["symbol"])
        weight = (mc / total_cap) if (mc and total_cap > 0) else 0.0
        result.append({**c, "weight": weight, "market_cap": mc})

    # Fallback: if all weights are 0 (no cap data), use equal weighting
    if total_cap == 0 and result:
        n = len(result)
        result = [{**c, "weight": 1.0 / n} for c in result]
        logger.warning("No market cap data available; using equal weighting.")

    return result
