from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.models import Portfolio, Position, TaxLot, Transaction
from .finnhub_client import finnhub_client
from .sp500_data import SP500_CONSTITUENTS

class PortfolioService:

    async def create_portfolio(
        self, db: AsyncSession, name: str, initial_value: float, client_id: int | None = None
    ) -> Portfolio:
        portfolio = Portfolio(name=name, initial_value=initial_value, cash=initial_value, client_id=client_id)
        db.add(portfolio)
        await db.commit()
        await db.refresh(portfolio)
        return portfolio

    async def list_portfolios(self, db: AsyncSession, advisor_id: int | None = None) -> list[dict]:
        stmt = select(Portfolio).order_by(Portfolio.created_at.desc())
        if advisor_id is not None:
            from ..models.models import Client
            stmt = stmt.join(Client, Portfolio.client_id == Client.id).where(
                Client.advisor_id == advisor_id
            )
        result = await db.execute(stmt)
        portfolios = result.scalars().all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "initial_value": p.initial_value,
                "cash": p.cash,
                "client_id": p.client_id,
                "created_at": p.created_at.isoformat(),
            }
            for p in portfolios
        ]

    async def construct_portfolio(
        self,
        db: AsyncSession,
        portfolio_id: int,
        excluded_sectors: list[str] = None,
        excluded_symbols: list[str] = None,
    ) -> dict:
        portfolio = await db.get(Portfolio, portfolio_id)
        if not portfolio:
            raise ValueError("Portfolio not found")

        excluded_sectors = [s.strip() for s in (excluded_sectors or [])]
        excluded_symbols = [s.strip().upper() for s in (excluded_symbols or [])]

        eligible = [
            c for c in SP500_CONSTITUENTS
            if c["sector"] not in excluded_sectors and c["symbol"] not in excluded_symbols
        ]

        total_weight = sum(c["weight"] for c in eligible)
        symbols = [c["symbol"] for c in eligible]
        quotes = await finnhub_client.get_multiple_quotes(symbols)

        available_cash = portfolio.cash
        total_invested = 0.0
        positions_created = []

        for constituent in eligible:
            symbol = constituent["symbol"]
            quote = quotes.get(symbol, {})
            price = quote.get("current_price", 0)
            if price <= 0:
                continue

            normalized_weight = constituent["weight"] / total_weight
            target_value = available_cash * normalized_weight
            shares = round(target_value / price, 6)
            if shares < 1e-6:
                continue

            cost = shares * price
            if total_invested + cost > available_cash:
                shares = round((available_cash - total_invested) / price, 6)
                if shares < 1e-6:
                    continue
                cost = shares * price

            position = Position(
                portfolio_id=portfolio_id,
                symbol=symbol,
                name=constituent["name"],
                sector=constituent["sector"],
                shares=float(shares),
                avg_cost_basis=price,
                target_weight=normalized_weight,
            )
            db.add(position)
            await db.flush()

            tax_lot = TaxLot(
                position_id=position.id,
                shares=float(shares),
                cost_basis=price,
                purchase_date=datetime.utcnow(),
            )
            db.add(tax_lot)

            transaction = Transaction(
                portfolio_id=portfolio_id,
                symbol=symbol,
                transaction_type="BUY",
                shares=float(shares),
                price=price,
                total_value=cost,
                notes="Initial portfolio construction",
            )
            db.add(transaction)

            total_invested += cost
            positions_created.append({
                "symbol": symbol,
                "shares": shares,
                "price": price,
                "value": cost,
                "weight": normalized_weight,
            })

        portfolio.cash = available_cash - total_invested
        await db.commit()

        return {
            "positions_created": len(positions_created),
            "total_invested": total_invested,
            "remaining_cash": portfolio.cash,
            "positions": positions_created,
        }

    async def get_portfolio_with_prices(self, db: AsyncSession, portfolio_id: int) -> dict:
        portfolio = await db.get(Portfolio, portfolio_id)
        if not portfolio:
            raise ValueError("Portfolio not found")

        result = await db.execute(
            select(Position).where(
                Position.portfolio_id == portfolio_id,
                Position.is_active == True,
            )
        )
        positions = result.scalars().all()

        if not positions:
            return {
                "portfolio": {
                    "id": portfolio.id,
                    "name": portfolio.name,
                    "initial_value": portfolio.initial_value,
                    "cash": portfolio.cash,
                    "created_at": portfolio.created_at.isoformat(),
                },
                "positions": [],
                "total_value": portfolio.cash,
                "total_cost_basis": 0,
                "total_gain_loss": 0,
                "total_gain_loss_pct": 0,
            }

        symbols = [p.symbol for p in positions]
        quotes = await finnhub_client.get_multiple_quotes(symbols)

        total_market_value = portfolio.cash
        total_cost_basis = 0.0
        positions_data = []

        for pos in positions:
            quote = quotes.get(pos.symbol, {})
            price = quote.get("current_price") or pos.avg_cost_basis
            market_value = pos.shares * price
            cost_total = pos.shares * pos.avg_cost_basis
            unrealized_gl = market_value - cost_total
            unrealized_gl_pct = (unrealized_gl / cost_total * 100) if cost_total > 0 else 0

            total_market_value += market_value
            total_cost_basis += cost_total

            positions_data.append({
                "id": pos.id,
                "symbol": pos.symbol,
                "name": pos.name,
                "sector": pos.sector,
                "shares": pos.shares,
                "avg_cost_basis": pos.avg_cost_basis,
                "current_price": price,
                "market_value": market_value,
                "cost_basis_total": cost_total,
                "unrealized_gain_loss": unrealized_gl,
                "unrealized_gain_loss_pct": unrealized_gl_pct,
                "target_weight": pos.target_weight,
                "actual_weight": 0,
                "change_percent": quote.get("change_percent", 0),
            })

        for p in positions_data:
            p["actual_weight"] = p["market_value"] / total_market_value if total_market_value > 0 else 0

        total_gain_loss = total_market_value - portfolio.initial_value
        total_gain_loss_pct = (total_gain_loss / portfolio.initial_value * 100) if portfolio.initial_value > 0 else 0

        return {
            "portfolio": {
                "id": portfolio.id,
                "name": portfolio.name,
                "initial_value": portfolio.initial_value,
                "cash": portfolio.cash,
                "created_at": portfolio.created_at.isoformat(),
            },
            "positions": positions_data,
            "total_value": total_market_value,
            "total_cost_basis": total_cost_basis,
            "total_gain_loss": total_gain_loss,
            "total_gain_loss_pct": total_gain_loss_pct,
        }

    async def get_sector_allocation(self, db: AsyncSession, portfolio_id: int) -> list[dict]:
        result = await db.execute(
            select(Position).where(
                Position.portfolio_id == portfolio_id,
                Position.is_active == True,
            )
        )
        positions = result.scalars().all()

        symbols = [p.symbol for p in positions]
        quotes = await finnhub_client.get_multiple_quotes(symbols)

        sector_data: dict[str, dict] = {}
        total_value = 0.0

        for pos in positions:
            quote = quotes.get(pos.symbol, {})
            price = quote.get("current_price") or pos.avg_cost_basis
            value = pos.shares * price
            total_value += value
            sector = pos.sector or "Unknown"
            if sector not in sector_data:
                sector_data[sector] = {"sector": sector, "value": 0.0, "positions": []}
            sector_data[sector]["value"] += value
            sector_data[sector]["positions"].append(pos.symbol)

        output = []
        for sector, data in sector_data.items():
            data["weight"] = data["value"] / total_value if total_value > 0 else 0
            output.append(data)

        return sorted(output, key=lambda x: x["value"], reverse=True)

    async def get_transactions(self, db: AsyncSession, portfolio_id: int) -> list[dict]:
        result = await db.execute(
            select(Transaction)
            .where(Transaction.portfolio_id == portfolio_id)
            .order_by(Transaction.timestamp.desc())
        )
        txns = result.scalars().all()
        return [
            {
                "id": t.id,
                "symbol": t.symbol,
                "type": t.transaction_type,
                "shares": t.shares,
                "price": t.price,
                "total_value": t.total_value,
                "timestamp": t.timestamp.isoformat(),
                "notes": t.notes,
            }
            for t in txns
        ]

portfolio_service = PortfolioService()
