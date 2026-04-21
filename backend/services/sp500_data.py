"""
sp500_data.py — public surface for index constituent data.

Preserved public API:
  SP500_CONSTITUENTS        list[dict]  – active S&P 500 rows
  NASDAQ100_CONSTITUENTS    list[dict]  – active NASDAQ-100 rows
  SECTOR_ALTERNATIVES       dict        – top symbols per GICS sector (for TLH)
  SP500_SYMBOL_MAP          dict        – symbol → constituent dict
  INDEX_MAP                 dict        – index key → {constituents, annual_returns}
  NASDAQ100_ANNUAL_RETURNS  dict        – year → float (static historical data)

Data is loaded lazily:
  1. Try DB (via constituent_store.load_snapshot which reads the JSON snapshot).
  2. Fall back to bundled JSON snapshot if DB is cold.
  3. Fall back to hardcoded stubs if snapshot is missing (first-ever cold start).

The DB-backed lists are refreshed in the background on app startup (see main.py).
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static historical data (not constituent data — keep as-is)
# ---------------------------------------------------------------------------

NASDAQ100_ANNUAL_RETURNS: dict[int, float] = {
    2015: 0.0978, 2016: 0.0700, 2017: 0.3270, 2018: -0.0118,
    2019: 0.3896, 2020: 0.4793, 2021: 0.2706, 2022: -0.3289,
    2023: 0.5391, 2024: 0.2490, 2025: -0.1050, 2026: 0.09,
}

# ---------------------------------------------------------------------------
# Hardcoded bootstrap stubs — used only if snapshot is unavailable
# ---------------------------------------------------------------------------

_SP500_STUB: list[dict] = [
    {"symbol": "AAPL",  "name": "Apple Inc.",               "weight": 0.0712, "sector": "Information Technology",  "industry": "Technology Hardware, Storage & Peripherals"},
    {"symbol": "MSFT",  "name": "Microsoft Corp.",           "weight": 0.0628, "sector": "Information Technology",  "industry": "Systems Software"},
    {"symbol": "NVDA",  "name": "NVIDIA Corp.",              "weight": 0.0565, "sector": "Information Technology",  "industry": "Semiconductors"},
    {"symbol": "AMZN",  "name": "Amazon.com Inc.",           "weight": 0.0371, "sector": "Consumer Discretionary",  "industry": "Broadline Retail"},
    {"symbol": "META",  "name": "Meta Platforms Inc.",       "weight": 0.0257, "sector": "Communication Services",  "industry": "Interactive Media & Services"},
    {"symbol": "GOOGL", "name": "Alphabet Inc. Class A",     "weight": 0.0196, "sector": "Communication Services",  "industry": "Interactive Media & Services"},
    {"symbol": "GOOG",  "name": "Alphabet Inc. Class C",     "weight": 0.0168, "sector": "Communication Services",  "industry": "Interactive Media & Services"},
    {"symbol": "LLY",   "name": "Eli Lilly and Co.",         "weight": 0.0148, "sector": "Health Care",             "industry": "Pharmaceuticals"},
    {"symbol": "JPM",   "name": "JPMorgan Chase & Co.",      "weight": 0.0142, "sector": "Financials",              "industry": "Diversified Banks"},
    {"symbol": "TSLA",  "name": "Tesla Inc.",                "weight": 0.0129, "sector": "Consumer Discretionary",  "industry": "Automobile Manufacturers"},
    {"symbol": "XOM",   "name": "Exxon Mobil Corp.",         "weight": 0.0124, "sector": "Energy",                  "industry": "Integrated Oil & Gas"},
    {"symbol": "UNH",   "name": "UnitedHealth Group Inc.",   "weight": 0.0114, "sector": "Health Care",             "industry": "Managed Health Care"},
    {"symbol": "V",     "name": "Visa Inc.",                 "weight": 0.0110, "sector": "Financials",              "industry": "Transaction & Payment Processing Services"},
    {"symbol": "AVGO",  "name": "Broadcom Inc.",             "weight": 0.0108, "sector": "Information Technology",  "industry": "Semiconductors"},
    {"symbol": "MA",    "name": "Mastercard Inc.",           "weight": 0.0096, "sector": "Financials",              "industry": "Transaction & Payment Processing Services"},
    {"symbol": "COST",  "name": "Costco Wholesale Corp.",    "weight": 0.0091, "sector": "Consumer Staples",        "industry": "Consumer Staples Merchandise Retail"},
    {"symbol": "PG",    "name": "Procter & Gamble Co.",      "weight": 0.0082, "sector": "Consumer Staples",        "industry": "Personal Care Products"},
    {"symbol": "JNJ",   "name": "Johnson & Johnson",         "weight": 0.0078, "sector": "Health Care",             "industry": "Pharmaceuticals"},
    {"symbol": "HD",    "name": "Home Depot Inc.",           "weight": 0.0076, "sector": "Consumer Discretionary",  "industry": "Home Improvement Retail"},
    {"symbol": "WMT",   "name": "Walmart Inc.",              "weight": 0.0075, "sector": "Consumer Staples",        "industry": "Consumer Staples Merchandise Retail"},
    {"symbol": "ABBV",  "name": "AbbVie Inc.",               "weight": 0.0071, "sector": "Health Care",             "industry": "Biotechnology"},
    {"symbol": "BAC",   "name": "Bank of America Corp.",     "weight": 0.0068, "sector": "Financials",              "industry": "Diversified Banks"},
    {"symbol": "NFLX",  "name": "Netflix Inc.",              "weight": 0.0066, "sector": "Communication Services",  "industry": "Movies & Entertainment"},
    {"symbol": "AMD",   "name": "Advanced Micro Devices",    "weight": 0.0063, "sector": "Information Technology",  "industry": "Semiconductors"},
    {"symbol": "KO",    "name": "Coca-Cola Co.",             "weight": 0.0061, "sector": "Consumer Staples",        "industry": "Soft Drinks & Non-alcoholic Beverages"},
    {"symbol": "CRM",   "name": "Salesforce Inc.",           "weight": 0.0059, "sector": "Information Technology",  "industry": "Application Software"},
    {"symbol": "PEP",   "name": "PepsiCo Inc.",              "weight": 0.0057, "sector": "Consumer Staples",        "industry": "Soft Drinks & Non-alcoholic Beverages"},
    {"symbol": "ORCL",  "name": "Oracle Corp.",              "weight": 0.0055, "sector": "Information Technology",  "industry": "Application Software"},
    {"symbol": "TMO",   "name": "Thermo Fisher Scientific",  "weight": 0.0053, "sector": "Health Care",             "industry": "Life Sciences Tools & Services"},
    {"symbol": "ACN",   "name": "Accenture PLC",             "weight": 0.0051, "sector": "Information Technology",  "industry": "IT Consulting & Other Services"},
    {"symbol": "MRK",   "name": "Merck & Co. Inc.",          "weight": 0.0049, "sector": "Health Care",             "industry": "Pharmaceuticals"},
    {"symbol": "CVX",   "name": "Chevron Corp.",             "weight": 0.0048, "sector": "Energy",                  "industry": "Integrated Oil & Gas"},
    {"symbol": "WFC",   "name": "Wells Fargo & Co.",         "weight": 0.0047, "sector": "Financials",              "industry": "Diversified Banks"},
    {"symbol": "CSCO",  "name": "Cisco Systems Inc.",        "weight": 0.0046, "sector": "Information Technology",  "industry": "Communications Equipment"},
    {"symbol": "ABT",   "name": "Abbott Laboratories",       "weight": 0.0044, "sector": "Health Care",             "industry": "Health Care Equipment"},
    {"symbol": "GS",    "name": "Goldman Sachs Group",       "weight": 0.0043, "sector": "Financials",              "industry": "Investment Banking & Brokerage"},
    {"symbol": "MS",    "name": "Morgan Stanley",            "weight": 0.0042, "sector": "Financials",              "industry": "Investment Banking & Brokerage"},
    {"symbol": "NOW",   "name": "ServiceNow Inc.",           "weight": 0.0041, "sector": "Information Technology",  "industry": "Application Software"},
    {"symbol": "LIN",   "name": "Linde PLC",                 "weight": 0.0040, "sector": "Materials",               "industry": "Industrial Gases"},
    {"symbol": "ISRG",  "name": "Intuitive Surgical Inc.",   "weight": 0.0039, "sector": "Health Care",             "industry": "Health Care Equipment"},
    {"symbol": "TXN",   "name": "Texas Instruments Inc.",    "weight": 0.0038, "sector": "Information Technology",  "industry": "Semiconductors"},
    {"symbol": "RTX",   "name": "RTX Corp.",                 "weight": 0.0037, "sector": "Industrials",             "industry": "Aerospace & Defense"},
    {"symbol": "QCOM",  "name": "Qualcomm Inc.",             "weight": 0.0036, "sector": "Information Technology",  "industry": "Semiconductors"},
    {"symbol": "BX",    "name": "Blackstone Inc.",           "weight": 0.0035, "sector": "Financials",              "industry": "Asset Management & Custody Banks"},
    {"symbol": "PM",    "name": "Philip Morris International","weight": 0.0034, "sector": "Consumer Staples",        "industry": "Tobacco"},
    {"symbol": "HON",   "name": "Honeywell International",   "weight": 0.0033, "sector": "Industrials",             "industry": "Industrial Conglomerates"},
    {"symbol": "SPGI",  "name": "S&P Global Inc.",           "weight": 0.0032, "sector": "Financials",              "industry": "Financial Exchanges & Data"},
    {"symbol": "CAT",   "name": "Caterpillar Inc.",          "weight": 0.0031, "sector": "Industrials",             "industry": "Construction Machinery & Heavy Transportation Equipment"},
    {"symbol": "AMGN",  "name": "Amgen Inc.",                "weight": 0.0030, "sector": "Health Care",             "industry": "Biotechnology"},
    {"symbol": "BLK",   "name": "BlackRock Inc.",            "weight": 0.0029, "sector": "Financials",              "industry": "Asset Management & Custody Banks"},
]

_NASDAQ100_STUB: list[dict] = [
    {"symbol": "AAPL",  "name": "Apple Inc.",               "weight": 0.0900, "sector": "Information Technology",  "industry": ""},
    {"symbol": "MSFT",  "name": "Microsoft Corp.",           "weight": 0.0800, "sector": "Information Technology",  "industry": ""},
    {"symbol": "NVDA",  "name": "NVIDIA Corp.",             "weight": 0.0720, "sector": "Information Technology",  "industry": ""},
    {"symbol": "AMZN",  "name": "Amazon.com Inc.",          "weight": 0.0520, "sector": "Consumer Discretionary",  "industry": ""},
    {"symbol": "META",  "name": "Meta Platforms Inc.",      "weight": 0.0460, "sector": "Communication Services",  "industry": ""},
    {"symbol": "GOOGL", "name": "Alphabet Inc. Class A",    "weight": 0.0310, "sector": "Communication Services",  "industry": ""},
    {"symbol": "GOOG",  "name": "Alphabet Inc. Class C",    "weight": 0.0295, "sector": "Communication Services",  "industry": ""},
    {"symbol": "TSLA",  "name": "Tesla Inc.",               "weight": 0.0310, "sector": "Consumer Discretionary",  "industry": ""},
    {"symbol": "AVGO",  "name": "Broadcom Inc.",            "weight": 0.0280, "sector": "Information Technology",  "industry": ""},
    {"symbol": "COST",  "name": "Costco Wholesale Corp.",   "weight": 0.0260, "sector": "Consumer Staples",        "industry": ""},
    {"symbol": "NFLX",  "name": "Netflix Inc.",             "weight": 0.0220, "sector": "Communication Services",  "industry": ""},
    {"symbol": "AMD",   "name": "Advanced Micro Devices",   "weight": 0.0200, "sector": "Information Technology",  "industry": ""},
    {"symbol": "QCOM",  "name": "Qualcomm Inc.",            "weight": 0.0180, "sector": "Information Technology",  "industry": ""},
    {"symbol": "CSCO",  "name": "Cisco Systems Inc.",       "weight": 0.0170, "sector": "Information Technology",  "industry": ""},
    {"symbol": "INTU",  "name": "Intuit Inc.",              "weight": 0.0160, "sector": "Information Technology",  "industry": ""},
    {"symbol": "TXN",   "name": "Texas Instruments Inc.",   "weight": 0.0155, "sector": "Information Technology",  "industry": ""},
    {"symbol": "AMGN",  "name": "Amgen Inc.",               "weight": 0.0150, "sector": "Health Care",             "industry": ""},
    {"symbol": "ISRG",  "name": "Intuitive Surgical Inc.",  "weight": 0.0145, "sector": "Health Care",             "industry": ""},
    {"symbol": "AMAT",  "name": "Applied Materials Inc.",   "weight": 0.0140, "sector": "Information Technology",  "industry": ""},
    {"symbol": "MU",    "name": "Micron Technology Inc.",   "weight": 0.0135, "sector": "Information Technology",  "industry": ""},
    {"symbol": "LRCX",  "name": "Lam Research Corp.",       "weight": 0.0130, "sector": "Information Technology",  "industry": ""},
    {"symbol": "NOW",   "name": "ServiceNow Inc.",          "weight": 0.0125, "sector": "Information Technology",  "industry": ""},
    {"symbol": "ADI",   "name": "Analog Devices Inc.",      "weight": 0.0120, "sector": "Information Technology",  "industry": ""},
    {"symbol": "KLAC",  "name": "KLA Corp.",                "weight": 0.0115, "sector": "Information Technology",  "industry": ""},
    {"symbol": "PANW",  "name": "Palo Alto Networks",       "weight": 0.0110, "sector": "Information Technology",  "industry": ""},
    {"symbol": "SNPS",  "name": "Synopsys Inc.",            "weight": 0.0105, "sector": "Information Technology",  "industry": ""},
    {"symbol": "CDNS",  "name": "Cadence Design Systems",   "weight": 0.0100, "sector": "Information Technology",  "industry": ""},
    {"symbol": "CRWD",  "name": "CrowdStrike Holdings",     "weight": 0.0095, "sector": "Information Technology",  "industry": ""},
    {"symbol": "MRVL",  "name": "Marvell Technology",       "weight": 0.0090, "sector": "Information Technology",  "industry": ""},
    {"symbol": "ASML",  "name": "ASML Holding N.V.",        "weight": 0.0085, "sector": "Information Technology",  "industry": ""},
    {"symbol": "ORLY",  "name": "O'Reilly Automotive",      "weight": 0.0080, "sector": "Consumer Discretionary",  "industry": ""},
    {"symbol": "FTNT",  "name": "Fortinet Inc.",            "weight": 0.0075, "sector": "Information Technology",  "industry": ""},
    {"symbol": "MELI",  "name": "MercadoLibre Inc.",        "weight": 0.0070, "sector": "Consumer Discretionary",  "industry": ""},
    {"symbol": "MDLZ",  "name": "Mondelez International",   "weight": 0.0065, "sector": "Consumer Staples",        "industry": ""},
    {"symbol": "ADP",   "name": "Automatic Data Processing","weight": 0.0060, "sector": "Information Technology",  "industry": ""},
    {"symbol": "GILD",  "name": "Gilead Sciences Inc.",     "weight": 0.0058, "sector": "Health Care",             "industry": ""},
    {"symbol": "REGN",  "name": "Regeneron Pharmaceuticals","weight": 0.0055, "sector": "Health Care",             "industry": ""},
    {"symbol": "VRTX",  "name": "Vertex Pharmaceuticals",   "weight": 0.0052, "sector": "Health Care",             "industry": ""},
    {"symbol": "IDXX",  "name": "IDEXX Laboratories",       "weight": 0.0048, "sector": "Health Care",             "industry": ""},
    {"symbol": "DXCM",  "name": "DexCom Inc.",              "weight": 0.0045, "sector": "Health Care",             "industry": ""},
]


# ---------------------------------------------------------------------------
# Lazy loader
# ---------------------------------------------------------------------------

_SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "constituents_snapshot.json")


def _load_from_snapshot(index_name: str) -> list[dict]:
    """Load from the JSON snapshot file. Returns [] on any failure."""
    try:
        if not os.path.exists(_SNAPSHOT_PATH):
            return []
        with open(_SNAPSHOT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get(index_name, [])
        if rows:
            logger.debug("Loaded %d %s rows from snapshot.", len(rows), index_name)
        return rows
    except Exception as exc:
        logger.warning("Could not load snapshot for %s: %s", index_name, exc)
        return []


TOP_N = 20  # keep in sync with constituent_store.TOP_N


def _cap_top_n(rows: list[dict]) -> list[dict]:
    """Take the top-N by weight and renormalize so the truncated set sums to 1.0."""
    if not rows:
        return rows
    sorted_rows = sorted(rows, key=lambda c: c.get("weight", 0.0) or 0.0, reverse=True)
    top = sorted_rows[:TOP_N]
    total = sum((c.get("weight") or 0.0) for c in top)
    if total > 0:
        for c in top:
            c["weight"] = (c.get("weight") or 0.0) / total
    return top


def _build_constituents(index_name: str, stub: list[dict]) -> list[dict]:
    """Return top-N live data from snapshot, or fall back to top-N of the stub."""
    rows = _load_from_snapshot(index_name)
    if rows:
        return _cap_top_n(rows)
    logger.warning(
        "No snapshot data for %s; using hardcoded stub (%d rows).", index_name, len(stub)
    )
    return _cap_top_n(stub)


# ---------------------------------------------------------------------------
# Public API — computed lazily at first import
# ---------------------------------------------------------------------------

SP500_CONSTITUENTS: list[dict] = _build_constituents("sp500", _SP500_STUB)
NASDAQ100_CONSTITUENTS: list[dict] = _build_constituents("nasdaq100", _NASDAQ100_STUB)

SP500_SYMBOL_MAP: dict[str, dict] = {c["symbol"]: c for c in SP500_CONSTITUENTS}


def _derive_sector_alternatives(constituents: list[dict], top_n: int = 12) -> dict[str, list[str]]:
    """
    Build SECTOR_ALTERNATIVES from the live S&P 500 data.
    Returns top_n symbols by weight per GICS sector.
    """
    sector_map: dict[str, list[dict]] = {}
    for c in constituents:
        sector = c.get("sector", "Unknown") or "Unknown"
        sector_map.setdefault(sector, []).append(c)

    result: dict[str, list[str]] = {}
    for sector, members in sector_map.items():
        sorted_members = sorted(members, key=lambda x: x.get("weight", 0.0), reverse=True)
        result[sector] = [m["symbol"] for m in sorted_members[:top_n]]

    return result


SECTOR_ALTERNATIVES: dict[str, list[str]] = _derive_sector_alternatives(SP500_CONSTITUENTS)

INDEX_MAP: dict[str, dict] = {
    "sp500":    {"constituents": SP500_CONSTITUENTS,    "annual_returns": None},
    "nasdaq":   {"constituents": NASDAQ100_CONSTITUENTS, "annual_returns": NASDAQ100_ANNUAL_RETURNS},
    "russell1000": {"constituents": _build_constituents("russell1000", []), "annual_returns": None},
}
