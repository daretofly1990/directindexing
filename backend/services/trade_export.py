"""
Broker-specific CSV exports for an approved TradePlan.

Customers paste the resulting CSV into their broker's batch-trade importer.
Formats:
  - schwab   — Schwab "StreetSmart" batch order CSV (symbol, action, qty, order_type)
  - fidelity — Fidelity "Active Trader Pro" CSV
  - generic  — portable layout: symbol, action, shares, est_price, notes

Schwab/Fidelity column orders evolve; these formats match current public docs as of
2026-04. Update constants here if the brokers change their importer templates.
"""
import csv
import io
import json

from ..models.models import TradePlan


def _rows_from_plan(plan: TradePlan) -> list[dict]:
    rows = []
    for item in plan.items:
        rows.append({
            "symbol": item.symbol,
            "action": item.action,          # BUY / SELL
            "shares": item.shares,
            "est_price": item.est_price,
            "notes": item.notes or "",
            "lot_ids": json.loads(item.lot_ids_json) if item.lot_ids_json else [],
        })
    return rows


def export_schwab(plan: TradePlan) -> str:
    rows = _rows_from_plan(plan)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Symbol", "Action", "Quantity", "Order Type", "Limit Price", "Time in Force", "Notes"])
    for r in rows:
        w.writerow([
            r["symbol"],
            r["action"],
            round(r["shares"], 4),
            "MARKET" if not r["est_price"] else "LIMIT",
            round(r["est_price"], 2) if r["est_price"] else "",
            "DAY",
            (r["notes"] or "")[:80],
        ])
    return out.getvalue()


def export_fidelity(plan: TradePlan) -> str:
    rows = _rows_from_plan(plan)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Account", "Symbol", "Action", "Quantity", "Order Type", "Price", "Expiration"])
    for r in rows:
        w.writerow([
            "DEFAULT",
            r["symbol"],
            "Buy" if r["action"] == "BUY" else "Sell",
            round(r["shares"], 4),
            "Market" if not r["est_price"] else "Limit",
            round(r["est_price"], 2) if r["est_price"] else "",
            "Day",
        ])
    return out.getvalue()


def export_generic(plan: TradePlan) -> str:
    rows = _rows_from_plan(plan)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["symbol", "action", "shares", "est_price", "lot_ids", "notes"])
    for r in rows:
        w.writerow([
            r["symbol"],
            r["action"],
            round(r["shares"], 4),
            round(r["est_price"], 2) if r["est_price"] else "",
            ";".join(str(x) for x in r["lot_ids"]),
            r["notes"],
        ])
    return out.getvalue()


EXPORTERS = {
    "schwab": export_schwab,
    "fidelity": export_fidelity,
    "generic": export_generic,
}
