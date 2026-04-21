"""
Structured tax-loss harvesting primitives.

These functions are the callable building blocks for both:
  1. The Claude tool-use reasoning loop (tlh_agent.py)
  2. Traditional REST endpoints (direct calls from route handlers)
  3. The MCP server (mcp_server.py)

Each function is self-contained: takes a db session + portfolio_id + typed args,
returns a plain dict suitable for JSON serialisation.
"""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Portfolio, Position, TaxLot, Transaction
from .lot_engine import lot_engine, _is_long_term, LONG_TERM_DAYS, WASH_SALE_WINDOW
from .finnhub_client import finnhub_client

# ---------------------------------------------------------------------------
# ETF replacement table  (symbol → [ETF1, ETF2, …])
# Chosen to avoid substantially identical securities across fund families.
# Sector ETFs are safe replacements for individual stock losses.
# ---------------------------------------------------------------------------

_SECTOR_ETF_REPLACEMENTS: dict[str, list[str]] = {
    "Information Technology":       ["XLK",  "VGT",  "FTEC", "IETC"],
    "Technology":                   ["XLK",  "VGT",  "FTEC", "IETC"],
    "Health Care":                  ["XLV",  "VHT",  "IYH",  "FHLC"],
    "Healthcare":                   ["XLV",  "VHT",  "IYH",  "FHLC"],
    "Financials":                   ["XLF",  "VFH",  "IYF",  "FNCL"],
    "Consumer Discretionary":       ["XLY",  "VCR",  "IYC"],
    "Consumer Staples":             ["XLP",  "VDC",  "IYK"],
    "Energy":                       ["XLE",  "VDE",  "IYE"],
    "Industrials":                  ["XLI",  "VIS",  "IYJ"],
    "Materials":                    ["XLB",  "VAW",  "IYM"],
    "Utilities":                    ["XLU",  "VPU",  "IDU"],
    "Communication Services":       ["XLC",  "VOX",  "IYZ"],
    "Real Estate":                  ["XLRE", "VNQ",  "IYR"],
}

# For broad market ETFs — switch to a non-identical fund tracking similar index
_ETF_REPLACEMENTS: dict[str, list[str]] = {
    "SPY":  ["VTI",  "SCHB", "ITOT"],   # S&P 500 → total market (not sub-identical)
    "IVV":  ["VTI",  "SCHB", "ITOT"],
    "VOO":  ["VTI",  "SCHB", "ITOT"],
    "QQQ":  ["VGT",  "XLK",  "FTEC"],   # NASDAQ-100 → tech sector
    "QQQM": ["VGT",  "XLK",  "FTEC"],
    "VTI":  ["SCHB", "ITOT", "IWV"],
    "SCHB": ["VTI",  "ITOT", "IWV"],
}


# ---------------------------------------------------------------------------
# 1. find_losses
# ---------------------------------------------------------------------------

async def find_losses(
    db: AsyncSession,
    portfolio_id: int,
    target_amount: float | None = None,
    min_loss_pct: float = 0.02,
    symbols: list[str] | None = None,
) -> dict:
    """
    Find positions with unrealized losses suitable for harvesting.

    Args:
        target_amount:  Stop when cumulative harvestable loss reaches this $ amount.
        min_loss_pct:   Minimum position-level loss % to include (default 2%).
        symbols:        If given, restrict search to these symbols.

    Returns dict with `opportunities` list sorted largest-loss-first,
    plus summary totals.
    """
    stmt = select(Position).where(
        Position.portfolio_id == portfolio_id,
        Position.is_active == True,  # noqa: E712
    )
    if symbols:
        upper = [s.upper() for s in symbols]
        stmt = stmt.where(Position.symbol.in_(upper))

    result = await db.execute(stmt)
    positions = result.scalars().all()

    if not positions:
        return {"opportunities": [], "total_harvestable_loss": 0.0, "currency": "USD"}

    price_map = await finnhub_client.get_multiple_quotes([p.symbol for p in positions])
    now = datetime.utcnow()

    # Symbols recently sold — wash-sale post-window
    wash_window = now - timedelta(days=WASH_SALE_WINDOW)
    sold_result = await db.execute(
        select(Transaction).where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.transaction_type.in_(["SELL", "HARVEST"]),
            Transaction.timestamp >= wash_window,
        )
    )
    recently_sold = {t.symbol for t in sold_result.scalars().all()}

    opportunities = []
    cumulative_loss = 0.0

    for pos in positions:
        if pos.symbol in recently_sold:
            continue

        quote = price_map.get(pos.symbol, {})
        price = quote.get("current_price") or pos.avg_cost_basis
        if price <= 0:
            continue

        position_loss_pct = (price - pos.avg_cost_basis) / pos.avg_cost_basis if pos.avg_cost_basis > 0 else 0.0
        if position_loss_pct > -min_loss_pct:
            continue

        open_lots = await lot_engine.get_open_lots(db, pos.id)
        lot_detail = []
        total_loss = 0.0

        for lot in open_lots:
            gl = (price - lot.cost_basis) * lot.shares
            if gl < 0:
                total_loss += gl
            lot_detail.append({
                "lot_id": lot.id,
                "shares": lot.shares,
                "cost_basis_per_share": lot.cost_basis,
                "current_price": price,
                "unrealized_gl": round(gl, 2),
                "holding_days": (now - lot.purchase_date).days,
                "is_long_term": _is_long_term(lot.purchase_date, now),
                "days_until_long_term": max(0, LONG_TERM_DAYS - (now - lot.purchase_date).days),
            })

        pre_buys = await lot_engine.check_wash_sale(db, portfolio_id, pos.symbol, now)
        replacements = _get_replacements(pos.symbol, pos.sector)

        opportunities.append({
            "position_id": pos.id,
            "symbol": pos.symbol,
            "sector": pos.sector,
            "total_shares": pos.shares,
            "avg_cost_basis": pos.avg_cost_basis,
            "current_price": price,
            "total_unrealized_loss": round(total_loss, 2),
            "position_loss_pct": round(position_loss_pct * 100, 2),
            "wash_sale_pre_trigger": len(pre_buys) > 0,
            "replacement_candidates": replacements,
            "lots": lot_detail,
        })
        cumulative_loss += total_loss

        if target_amount is not None and abs(cumulative_loss) >= target_amount:
            break

    opportunities.sort(key=lambda x: x["total_unrealized_loss"])

    return {
        "opportunities": opportunities,
        "total_harvestable_loss": round(cumulative_loss, 2),
        "wash_sale_restricted": sorted(recently_sold),
        "currency": "USD",
    }


# ---------------------------------------------------------------------------
# 2. simulate_sale
# ---------------------------------------------------------------------------

async def simulate_sale(
    db: AsyncSession,
    portfolio_id: int,
    lot_ids: list[int],
    override_price: float | None = None,
) -> dict:
    """
    Simulate selling specific lots (Spec-ID).

    Fetches current price from Finnhub unless override_price is given.
    Returns projected gain/loss, tax impact by rate, and wash-sale risk.
    Does NOT write to the database.
    """
    result = await db.execute(
        select(TaxLot).where(
            TaxLot.id.in_(lot_ids),
            TaxLot.sale_date == None,  # noqa: E711
            TaxLot.shares > 0,
        )
    )
    lots = result.scalars().all()

    found_ids = {l.id for l in lots}
    missing = [i for i in lot_ids if i not in found_ids]
    if missing:
        return {"error": f"Lots not found or already closed: {missing}"}

    pos_ids = list({l.position_id for l in lots})
    pos_result = await db.execute(select(Position).where(Position.id.in_(pos_ids)))
    pos_map = {p.id: p for p in pos_result.scalars().all()}

    symbols = list({pos_map[l.position_id].symbol for l in lots if l.position_id in pos_map})

    if override_price is not None and len(symbols) == 1:
        price_map = {symbols[0]: {"current_price": override_price}}
    else:
        price_map = await finnhub_client.get_multiple_quotes(symbols)

    now = datetime.utcnow()
    short_term_gl = 0.0
    long_term_gl = 0.0
    lot_detail = []

    for lot in lots:
        pos = pos_map.get(lot.position_id)
        symbol = pos.symbol if pos else "?"
        price = (price_map.get(symbol, {}).get("current_price") or lot.cost_basis)

        proceeds = lot.shares * price
        cost = lot.shares * lot.cost_basis
        gl = proceeds - cost
        is_lt = _is_long_term(lot.purchase_date, now)

        if is_lt:
            long_term_gl += gl
        else:
            short_term_gl += gl

        lot_detail.append({
            "lot_id": lot.id,
            "symbol": symbol,
            "shares": lot.shares,
            "cost_basis_per_share": lot.cost_basis,
            "current_price": price,
            "proceeds": round(proceeds, 2),
            "gain_loss": round(gl, 2),
            "is_long_term": is_lt,
            "holding_days": (now - lot.purchase_date).days,
        })

    # Wash-sale check for each symbol
    wash_sale_risks = {}
    for sym in symbols:
        pre_buys = await lot_engine.check_wash_sale(db, portfolio_id, sym, now)
        wash_sale_risks[sym] = len(pre_buys) > 0

    # Tax impact estimates (marginal rates)
    st_tax_rate = 0.37
    lt_tax_rate = 0.20
    tax_impact = round(
        (short_term_gl * st_tax_rate + long_term_gl * lt_tax_rate), 2
    )  # negative = tax savings from losses

    return {
        "lots": lot_detail,
        "short_term_gain_loss": round(short_term_gl, 2),
        "long_term_gain_loss": round(long_term_gl, 2),
        "total_gain_loss": round(short_term_gl + long_term_gl, 2),
        "estimated_tax_impact": tax_impact,
        "wash_sale_risks": wash_sale_risks,
        "note": "Simulation only — no trades executed.",
    }


# ---------------------------------------------------------------------------
# 3. check_wash_sale
# ---------------------------------------------------------------------------

async def check_wash_sale(
    db: AsyncSession,
    portfolio_id: int,
    symbol: str,
    window_days: int = 30,
) -> dict:
    """
    Check wash-sale status for a symbol.

    Returns:
      - pre_sale_risk: bought within window_days before today → selling now disallows loss
      - post_sale_blocked: sold within window_days → can't repurchase yet
      - safe_to_sell: no pre-sale wash-sale risk
      - safe_to_buy: not in post-sale block window
      - earliest_repurchase_date: when it's safe to buy again (if blocked)
    """
    now = datetime.utcnow()
    symbol = symbol.upper()

    pre_window_start = now - timedelta(days=window_days)
    pre_buys = await db.execute(
        select(Transaction).where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.symbol == symbol,
            Transaction.transaction_type.in_(["BUY"]),
            Transaction.timestamp >= pre_window_start,
            Transaction.timestamp <= now,
        )
    )
    pre_buy_txns = pre_buys.scalars().all()

    post_window_start = now - timedelta(days=window_days)
    post_sells = await db.execute(
        select(Transaction).where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.symbol == symbol,
            Transaction.transaction_type.in_(["SELL", "HARVEST"]),
            Transaction.timestamp >= post_window_start,
            Transaction.timestamp <= now,
        )
    )
    post_sell_txns = post_sells.scalars().all()

    earliest_repurchase = None
    if post_sell_txns:
        latest_sell = max(t.timestamp for t in post_sell_txns)
        earliest_repurchase = (latest_sell + timedelta(days=window_days + 1)).strftime("%Y-%m-%d")

    return {
        "symbol": symbol,
        "pre_sale_risk": len(pre_buy_txns) > 0,
        "pre_sale_buys": [
            {"date": t.timestamp.strftime("%Y-%m-%d"), "shares": t.shares, "price": t.price}
            for t in pre_buy_txns
        ],
        "post_sale_blocked": len(post_sell_txns) > 0,
        "earliest_repurchase_date": earliest_repurchase,
        "safe_to_sell": len(pre_buy_txns) == 0,
        "safe_to_buy": len(post_sell_txns) == 0,
        "window_days": window_days,
    }


# ---------------------------------------------------------------------------
# 4. propose_replacement
# ---------------------------------------------------------------------------

async def propose_replacement(
    symbol: str,
    avoid_symbols: list[str] | None = None,
    sector: str | None = None,
) -> dict:
    """
    Propose replacement securities to maintain market exposure after harvesting.

    For individual stocks: recommends sector ETFs (safe harbor from wash-sale).
    For broad ETFs: recommends similar-index ETFs from different fund families.

    Returns a list of candidates with rationale.
    """
    symbol = symbol.upper()
    avoid = {s.upper() for s in (avoid_symbols or [])} | {symbol}

    candidates = []

    # Check if it's a known ETF with a direct replacement table
    if symbol in _ETF_REPLACEMENTS:
        for repl in _ETF_REPLACEMENTS[symbol]:
            if repl not in avoid:
                candidates.append({
                    "symbol": repl,
                    "rationale": f"Tracks similar index to {symbol} but different fund family — avoids substantially identical security rule.",
                    "type": "etf_replacement",
                })

    # Sector-based replacements (works for both stocks and sector ETFs)
    sector_key = sector or ""
    for sector_name, etfs in _SECTOR_ETF_REPLACEMENTS.items():
        if sector_name.lower() in sector_key.lower() or sector_key.lower() in sector_name.lower():
            for etf in etfs:
                if etf not in avoid and not any(c["symbol"] == etf for c in candidates):
                    candidates.append({
                        "symbol": etf,
                        "rationale": f"Sector ETF ({sector_name}) — maintains exposure while avoiding substantially identical security rule.",
                        "type": "sector_etf",
                    })

    # If no sector match and not a known ETF, suggest broad market alternatives
    if not candidates:
        for repl in ["VTI", "SCHB", "ITOT"]:
            if repl not in avoid:
                candidates.append({
                    "symbol": repl,
                    "rationale": "Broad total-market ETF — maintains equity exposure across all sectors.",
                    "type": "broad_market",
                })

    return {
        "symbol": symbol,
        "sector": sector,
        "candidates": candidates[:5],
        "note": (
            "Replacements avoid the 30-day wash-sale window. "
            "Sector ETFs are generally safe; consult a tax advisor for your specific situation."
        ),
    }


# ---------------------------------------------------------------------------
# 5. draft_trade_list
# ---------------------------------------------------------------------------

async def draft_trade_list(
    db: AsyncSession,
    portfolio_id: int,
    harvests: list[dict],
    tax_rate_short: float = 0.37,
    tax_rate_long: float = 0.20,
) -> dict:
    """
    Compile a complete trade list from a harvest plan.

    Each harvest dict must have:
      - lot_ids: list[int]   specific lots to sell (Spec-ID)
      - replacement_symbol: str  what to buy after selling

    Returns a structured trade list with sells, buys, estimated tax savings,
    and a wash-sale compliance checklist. Nothing is executed.
    """
    portfolio = await db.get(Portfolio, portfolio_id)
    if not portfolio:
        raise ValueError("Portfolio not found")

    sells = []
    buys = []
    total_st_loss = 0.0
    total_lt_loss = 0.0
    compliance_issues = []

    for harvest in harvests:
        lot_ids = harvest.get("lot_ids", [])
        replacement = harvest.get("replacement_symbol", "").upper()

        if not lot_ids:
            continue

        sim = await simulate_sale(db, portfolio_id, lot_ids)
        if "error" in sim:
            compliance_issues.append({"issue": sim["error"], "lot_ids": lot_ids})
            continue

        for lot in sim["lots"]:
            sells.append({
                "action": "SELL",
                "symbol": lot["symbol"],
                "lot_id": lot["lot_id"],
                "shares": lot["shares"],
                "estimated_price": lot["current_price"],
                "estimated_proceeds": lot["proceeds"],
                "gain_loss": lot["gain_loss"],
                "is_long_term": lot["is_long_term"],
            })

        st = sim["short_term_gain_loss"]
        lt = sim["long_term_gain_loss"]
        if st < 0:
            total_st_loss += st
        if lt < 0:
            total_lt_loss += lt

        # Check wash-sale for each sold symbol
        sold_symbols = {lot["symbol"] for lot in sim["lots"]}
        for sym in sold_symbols:
            ws = await check_wash_sale(db, portfolio_id, sym)
            if ws["pre_sale_risk"]:
                compliance_issues.append({
                    "issue": f"Pre-sale wash-sale risk: {sym} was purchased within last 30 days",
                    "symbol": sym,
                    "severity": "warning",
                })
            if replacement.upper() == sym:
                compliance_issues.append({
                    "issue": f"Replacement symbol {replacement} is the same as harvested symbol — wash-sale triggered",
                    "symbol": sym,
                    "severity": "error",
                })

        if replacement:
            # Estimate purchase amount = proceeds from this batch
            estimated_proceeds = sum(l["estimated_proceeds"] for l in sells[-len(lot_ids):])
            buys.append({
                "action": "BUY",
                "symbol": replacement,
                "estimated_amount": round(estimated_proceeds, 2),
                "note": f"Replacement for {', '.join(sold_symbols)}. Wait until order settles.",
            })

    estimated_tax_savings = round(
        abs(total_st_loss) * tax_rate_short + abs(total_lt_loss) * tax_rate_long, 2
    )

    return {
        "portfolio_id": portfolio_id,
        "sells": sells,
        "buys": buys,
        "summary": {
            "total_sells": len(sells),
            "total_buys": len(buys),
            "total_short_term_loss": round(total_st_loss, 2),
            "total_long_term_loss": round(total_lt_loss, 2),
            "estimated_tax_savings": estimated_tax_savings,
        },
        "compliance": {
            "issues": compliance_issues,
            "has_errors": any(i.get("severity") == "error" for i in compliance_issues),
            "has_warnings": any(i.get("severity") == "warning" for i in compliance_issues),
        },
        "status": "DRAFT — requires user approval before execution",
    }


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _get_replacements(symbol: str, sector: str | None) -> list[str]:
    if symbol in _ETF_REPLACEMENTS:
        return _ETF_REPLACEMENTS[symbol][:3]
    sector_key = sector or ""
    for name, etfs in _SECTOR_ETF_REPLACEMENTS.items():
        if name.lower() in sector_key.lower() or sector_key.lower() in name.lower():
            return etfs[:3]
    return ["VTI", "SCHB", "ITOT"]
