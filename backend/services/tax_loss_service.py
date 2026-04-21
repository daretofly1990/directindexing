from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Portfolio, Position, TaxLot, Transaction
from .finnhub_client import finnhub_client
from .lot_engine import lot_engine, LotSelectionMethod, _is_long_term, LONG_TERM_DAYS, WASH_SALE_WINDOW
from .sp500_data import SECTOR_ALTERNATIVES
from ..config import settings


class TaxLossService:

    async def get_tax_loss_opportunities(
        self,
        db: AsyncSession,
        portfolio_id: int,
        lot_method: LotSelectionMethod = LotSelectionMethod.HIFO,
    ) -> dict:
        result = await db.execute(
            select(Position).where(
                Position.portfolio_id == portfolio_id,
                Position.is_active == True,  # noqa: E712
            )
        )
        positions = result.scalars().all()

        # Symbols sold/harvested within the last 30 days — can't repurchase (wash-sale post window)
        wash_window = datetime.utcnow() - timedelta(days=WASH_SALE_WINDOW)
        sell_result = await db.execute(
            select(Transaction).where(
                Transaction.portfolio_id == portfolio_id,
                Transaction.transaction_type.in_(["SELL", "HARVEST"]),
                Transaction.timestamp >= wash_window,
            )
        )
        recently_sold = {t.symbol for t in sell_result.scalars().all()}

        symbols = [p.symbol for p in positions]
        quotes = await finnhub_client.get_multiple_quotes(symbols)
        portfolio_symbols = set(symbols)
        now = datetime.utcnow()

        opportunities = []
        total_harvestable_loss = 0.0

        for pos in positions:
            if pos.symbol in recently_sold:
                continue

            quote = quotes.get(pos.symbol, {})
            price = quote.get("current_price") or pos.avg_cost_basis
            if price <= 0:
                continue

            # Check pre-sale wash-sale: were shares of this symbol bought in the last 30 days?
            pre_sale_buys = await lot_engine.check_wash_sale(db, portfolio_id, pos.symbol, now)
            wash_sale_pre_trigger = len(pre_sale_buys) > 0

            # Collect open lots and compute per-lot loss potential
            open_lots = await lot_engine.get_open_lots(db, pos.id)
            if not open_lots:
                continue

            lot_detail = []
            total_position_loss = 0.0

            for lot in open_lots:
                lot_price_diff = price - lot.cost_basis
                lot_gl = lot_price_diff * lot.shares
                loss_pct = lot_price_diff / lot.cost_basis if lot.cost_basis > 0 else 0
                is_lt = _is_long_term(lot.purchase_date, now)
                holding_days = (now - lot.purchase_date).days

                lot_detail.append({
                    "lot_id": lot.id,
                    "shares": lot.shares,
                    "cost_basis": lot.cost_basis,
                    "current_price": price,
                    "unrealized_gain_loss": lot_gl,
                    "loss_percent": loss_pct * 100,
                    "holding_days": holding_days,
                    "is_long_term": is_lt,
                    "days_until_long_term": max(0, LONG_TERM_DAYS - holding_days),
                    "wash_sale_risk": wash_sale_pre_trigger,
                })
                if lot_gl < 0:
                    total_position_loss += lot_gl

            # Only surface positions where the chosen method would harvest a loss
            # Use avg_cost_basis to determine overall position loss threshold
            position_loss_pct = (price - pos.avg_cost_basis) / pos.avg_cost_basis if pos.avg_cost_basis > 0 else 0
            if position_loss_pct > settings.TAX_LOSS_THRESHOLD:
                continue

            alternatives = self._get_alternatives(
                pos.symbol, pos.sector, recently_sold, portfolio_symbols
            )

            # Estimate tax savings: short-term losses saved at ~37%, long-term at ~20%
            short_loss = sum(
                d["unrealized_gain_loss"] for d in lot_detail
                if d["unrealized_gain_loss"] < 0 and not d["is_long_term"]
            )
            long_loss = sum(
                d["unrealized_gain_loss"] for d in lot_detail
                if d["unrealized_gain_loss"] < 0 and d["is_long_term"]
            )
            estimated_tax_savings = abs(short_loss) * 0.37 + abs(long_loss) * 0.20

            opportunities.append({
                "position_id": pos.id,
                "symbol": pos.symbol,
                "name": pos.name,
                "sector": pos.sector,
                "total_shares": pos.shares,
                "avg_cost_basis": pos.avg_cost_basis,
                "current_price": price,
                "total_unrealized_loss": total_position_loss,
                "position_loss_percent": position_loss_pct * 100,
                "short_term_loss": short_loss,
                "long_term_loss": long_loss,
                "estimated_tax_savings": estimated_tax_savings,
                "lot_selection_method": lot_method,
                "lot_detail": lot_detail,
                "replacement_candidates": alternatives,
                "wash_sale_pre_trigger": wash_sale_pre_trigger,
            })
            total_harvestable_loss += total_position_loss

        opportunities.sort(key=lambda x: x["total_unrealized_loss"])

        return {
            "opportunities": opportunities,
            "total_harvestable_loss": total_harvestable_loss,
            "estimated_total_tax_savings": sum(o["estimated_tax_savings"] for o in opportunities),
            "wash_sale_restricted_symbols": list(recently_sold),
            "lot_selection_method": lot_method,
        }

    def _get_alternatives(
        self, symbol: str, sector: str, excluded: set, portfolio_symbols: set
    ) -> list[str]:
        alts = SECTOR_ALTERNATIVES.get(sector, [])
        return [s for s in alts if s != symbol and s not in excluded and s in portfolio_symbols][:3]

    async def execute_harvest(
        self,
        db: AsyncSession,
        portfolio_id: int,
        position_id: int,
        lot_method: LotSelectionMethod = LotSelectionMethod.HIFO,
    ) -> dict:
        pos = await db.get(Position, position_id)
        if not pos or pos.portfolio_id != portfolio_id:
            raise ValueError("Position not found")

        quote = await finnhub_client.get_quote(pos.symbol)
        price = quote.get("current_price") or pos.avg_cost_basis
        now = datetime.utcnow()

        # Pre-sale wash-sale check: did we buy this symbol in the last 30 days?
        pre_sale_buys = await lot_engine.check_wash_sale(db, portfolio_id, pos.symbol, now)
        wash_sale_triggered = len(pre_sale_buys) > 0

        # Use lot engine to close lots with chosen method
        close_result = await lot_engine.close_lots(
            db=db,
            position_id=position_id,
            shares_to_sell=pos.shares,
            sale_price=price,
            sale_date=now,
            method=lot_method,
        )

        proceeds = sum(d["proceeds"] for d in close_result["closed_lots"])
        economic_loss = close_result["total_gain"]  # negative means loss

        # If wash sale triggered, disallow the loss on these lots
        disallowed_amount = 0.0
        if wash_sale_triggered and economic_loss < 0:
            disallowed_amount = abs(economic_loss)
            await lot_engine.disallow_loss_on_lots(db, position_id, disallowed_amount, now)

        pos.is_active = False

        notes = (
            f"Tax-loss harvest via {lot_method}. "
            f"ST gain/loss: ${close_result['short_term_gain']:,.2f}, "
            f"LT gain/loss: ${close_result['long_term_gain']:,.2f}."
        )
        if wash_sale_triggered:
            notes += f" WASH SALE: ${disallowed_amount:,.2f} loss disallowed."

        transaction = Transaction(
            portfolio_id=portfolio_id,
            symbol=pos.symbol,
            transaction_type="HARVEST",
            shares=pos.shares,
            price=price,
            total_value=proceeds,
            notes=notes,
        )
        db.add(transaction)

        portfolio = await db.get(Portfolio, portfolio_id)
        portfolio.cash += proceeds

        await db.commit()

        recognizable_loss = economic_loss + disallowed_amount  # less negative if wash sale
        st_savings = abs(close_result["short_term_gain"]) * 0.37 if close_result["short_term_gain"] < 0 else 0
        lt_savings = abs(close_result["long_term_gain"]) * 0.20 if close_result["long_term_gain"] < 0 else 0

        return {
            "symbol": pos.symbol,
            "shares_sold": pos.shares,
            "sale_price": price,
            "proceeds": proceeds,
            "economic_gain_loss": economic_loss,
            "short_term_gain_loss": close_result["short_term_gain"],
            "long_term_gain_loss": close_result["long_term_gain"],
            "wash_sale_triggered": wash_sale_triggered,
            "wash_sale_disallowed": disallowed_amount,
            "recognizable_gain_loss": recognizable_loss,
            "estimated_tax_savings": st_savings + lt_savings,
            "lot_method": lot_method,
            "closed_lots": close_result["closed_lots"],
        }


tax_loss_service = TaxLossService()
