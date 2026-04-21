import math
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.models import Portfolio, Position, TaxLot, Transaction
from .finnhub_client import finnhub_client
from .lot_engine import lot_engine, LotSelectionMethod
from ..config import settings

class RebalancingService:

    async def get_rebalancing_recommendations(self, db: AsyncSession, portfolio_id: int) -> dict:
        portfolio = await db.get(Portfolio, portfolio_id)
        result = await db.execute(
            select(Position).where(
                Position.portfolio_id == portfolio_id,
                Position.is_active == True,
            )
        )
        positions = result.scalars().all()
        symbols = [p.symbol for p in positions]
        quotes = await finnhub_client.get_multiple_quotes(symbols)

        total_value = portfolio.cash
        pos_data = {}
        for pos in positions:
            quote = quotes.get(pos.symbol, {})
            price = quote.get("current_price") or pos.avg_cost_basis
            value = pos.shares * price
            total_value += value
            pos_data[pos.symbol] = {
                "position": pos, "price": price,
                "current_value": value, "target_weight": pos.target_weight,
            }

        trades = []
        total_drift = 0.0
        for symbol, data in pos_data.items():
            actual_weight = data["current_value"] / total_value if total_value > 0 else 0
            drift = actual_weight - data["target_weight"]
            total_drift += abs(drift)
            if abs(drift) >= settings.REBALANCE_THRESHOLD:
                target_value = total_value * data["target_weight"]
                value_diff = target_value - data["current_value"]
                shares_diff = value_diff / data["price"] if data["price"] > 0 else 0
                trades.append({
                    "symbol": symbol,
                    "name": data["position"].name,
                    "sector": data["position"].sector,
                    "action": "BUY" if shares_diff > 0 else "SELL",
                    "current_weight": round(actual_weight * 100, 2),
                    "target_weight": round(data["target_weight"] * 100, 2),
                    "drift": round(drift * 100, 2),
                    "shares_to_trade": round(abs(shares_diff), 4),
                    "estimated_value": round(abs(value_diff), 2),
                    "current_price": data["price"],
                })

        trades.sort(key=lambda x: abs(x["drift"]), reverse=True)

        return {
            "needs_rebalancing": len(trades) > 0,
            "total_drift": round(total_drift * 100, 2),
            "rebalance_threshold_pct": settings.REBALANCE_THRESHOLD * 100,
            "trades": trades,
            "portfolio_value": total_value,
            "cash_available": portfolio.cash,
            "positions_analyzed": len(positions),
        }

    async def execute_rebalancing(self, db: AsyncSession, portfolio_id: int) -> dict:
        recs = await self.get_rebalancing_recommendations(db, portfolio_id)
        portfolio = await db.get(Portfolio, portfolio_id)
        executed = []

        for trade in recs["trades"]:
            result = await db.execute(
                select(Position).where(
                    Position.portfolio_id == portfolio_id,
                    Position.symbol == trade["symbol"],
                    Position.is_active == True,
                )
            )
            pos = result.scalar_one_or_none()
            if not pos:
                continue

            quote = await finnhub_client.get_quote(trade["symbol"])
            price = quote.get("current_price") or trade["current_price"]
            shares = trade["shares_to_trade"]
            value = shares * price

            now = datetime.utcnow()
            if trade["action"] == "BUY" and portfolio.cash >= value:
                new_total_shares = pos.shares + shares
                pos.avg_cost_basis = (pos.avg_cost_basis * pos.shares + value) / new_total_shares
                pos.shares = new_total_shares
                portfolio.cash -= value
                await lot_engine.open_lot(db, pos.id, shares, price, now)
                db.add(Transaction(
                    portfolio_id=portfolio_id, symbol=trade["symbol"],
                    transaction_type="BUY", shares=shares, price=price,
                    total_value=value, notes="Rebalancing",
                ))
                executed.append({**trade, "executed_price": price})
            elif trade["action"] == "SELL":
                actual = min(shares, pos.shares)
                actual_value = actual * price
                close_result = await lot_engine.close_lots(
                    db, pos.id, actual, price, now,
                    method=LotSelectionMethod.MIN_TERM,
                )
                pos.shares -= actual
                if pos.shares <= 1e-9:
                    pos.is_active = False
                portfolio.cash += actual_value
                notes = (
                    f"Rebalancing. ST: ${close_result['short_term_gain']:,.2f}, "
                    f"LT: ${close_result['long_term_gain']:,.2f}"
                )
                db.add(Transaction(
                    portfolio_id=portfolio_id, symbol=trade["symbol"],
                    transaction_type="SELL", shares=actual, price=price,
                    total_value=actual_value, notes=notes,
                ))
                executed.append({**trade, "executed_price": price,
                                  "short_term_gain": close_result["short_term_gain"],
                                  "long_term_gain": close_result["long_term_gain"]})

        await db.commit()
        return {"trades_executed": len(executed), "trades": executed, "new_cash_balance": portfolio.cash}

rebalancing_service = RebalancingService()
