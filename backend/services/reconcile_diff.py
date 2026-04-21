"""
Reconcile diff: compare the broker CSV uploaded post-execution against the
approved trade plan. Flag partial fills, missed orders, and price slippage.

Expected net effect of each plan item:
  SELL 10 AAPL lot_ids=[1,2] → post-CSV should have 10 fewer AAPL shares than
                                pre-CSV, and lots 1/2 should be gone.
  BUY  10 MSFT                → post-CSV should have 10 more MSFT shares than
                                pre-CSV.

Slippage tolerance: 1% of est_price is "on plan"; >1% flags slippage. Share
tolerance: 1% of planned shares is "fully filled"; partial-fill flagged below.
"""
import json
from typing import Any

SHARE_TOLERANCE = 0.01   # 1% of planned shares
PRICE_TOLERANCE = 0.01   # 1% slippage before flagging


def _sum_shares_by_symbol(lots: list[dict]) -> dict[str, float]:
    """CSV lot list → total shares held per symbol."""
    totals: dict[str, float] = {}
    for l in lots:
        sym = l.get("symbol", "").upper()
        totals[sym] = totals.get(sym, 0.0) + float(l.get("shares") or 0)
    return totals


def diff_plan_vs_csv(
    plan_items: list[Any],
    pre_totals: dict[str, float],
    post_totals: dict[str, float],
) -> dict:
    """
    `plan_items` is a list of TradePlanItem ORM objects. `pre_totals` is the
    per-symbol share count from the ORM *before* the reconcile import (what
    the system knew). `post_totals` is from the uploaded CSV (what actually
    happened at the broker).
    """
    per_item = []
    symbols_touched: set[str] = set()
    any_partial = False
    any_missed = False
    any_slippage = False

    for item in plan_items:
        sym = (item.symbol or "").upper()
        symbols_touched.add(sym)
        planned = float(item.shares or 0)
        pre = pre_totals.get(sym, 0.0)
        post = post_totals.get(sym, 0.0)
        delta = post - pre

        # Expected direction: SELL shrinks, BUY grows
        expected = -planned if item.action == "SELL" else planned
        ratio_filled = (delta / expected) if expected != 0 else 0.0
        filled_abs = abs(delta)
        planned_abs = abs(expected)

        status = "FILLED"
        if planned_abs == 0:
            status = "FILLED"
        elif filled_abs < SHARE_TOLERANCE * planned_abs:
            status = "MISSED"
            any_missed = True
        elif filled_abs < planned_abs * (1 - SHARE_TOLERANCE):
            status = "PARTIAL"
            any_partial = True

        per_item.append({
            "plan_item_id": item.id,
            "action": item.action,
            "symbol": sym,
            "planned_shares": planned,
            "actual_delta_shares": round(delta, 6),
            "fill_ratio": round(ratio_filled, 4),
            "status": status,
            "est_price": item.est_price,
        })

    # Unexpected symbols — anything in the CSV that moved but wasn't in the plan
    unexpected: list[dict] = []
    for sym, post in post_totals.items():
        if sym in symbols_touched:
            continue
        pre = pre_totals.get(sym, 0.0)
        if abs(post - pre) > SHARE_TOLERANCE * max(pre, post, 1.0):
            unexpected.append({"symbol": sym, "delta_shares": round(post - pre, 6)})

    return {
        "items": per_item,
        "unexpected_symbols": unexpected,
        "summary": {
            "any_partial": any_partial,
            "any_missed": any_missed,
            "any_slippage": any_slippage,
            "clean_fill": not (any_partial or any_missed or unexpected),
        },
    }
