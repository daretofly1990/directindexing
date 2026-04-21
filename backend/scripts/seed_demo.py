"""
Seed a demo portfolio.

Creates an individual user + self-client + one portfolio with a curated mix
of 20 positions, varied holding periods (ST / LT / near-crossover), and
realistic unrealized losers the AI advisor can harvest. Also drops a few
historical harvest transactions so the Reports tab isn't empty on first look.

Idempotent: if `--email` already exists, updates the portfolio in place. Use
`--reset` to drop and recreate.

Usage:
    python -m backend.scripts.seed_demo
    python -m backend.scripts.seed_demo --email demo@example.com --password demo12345
    python -m backend.scripts.seed_demo --reset
"""
import argparse
import asyncio
import random
from datetime import datetime, timedelta

from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from backend.database import AsyncSessionLocal, init_db
from backend.models.models import (
    User, Client, Portfolio, Position, TaxLot, Transaction, Acknowledgement,
)
from backend.services.user_service import create_individual_user
from backend.api.routes.acknowledgements import CURRENT_VERSIONS


# 20 positions with curated holding periods and prices that include losers
# so the advisor has something to harvest. Numbers are illustrative —
# treat as demo data, not market prices.
POSITIONS = [
    # (symbol, sector, shares, avg_cost, current_price, days_ago_purchased)
    ("AAPL", "Technology",              40,  180.0, 175.0, 500),   # LT, small loss
    ("MSFT", "Technology",              30,  420.0, 405.0, 400),   # LT, small loss
    ("GOOGL", "Communication Services", 50,  155.0, 140.0,  90),   # ST, 10% loss (harvestable)
    ("AMZN", "Consumer Discretionary",  25,  185.0, 160.0,  45),   # ST, 13% loss (harvestable)
    ("META", "Communication Services",  15,  520.0, 500.0, 350),   # near-LT crossover
    ("NVDA", "Technology",              10, 1200.0, 900.0,  60),   # ST, 25% loss (big harvest)
    ("TSLA", "Consumer Discretionary",  20,  240.0, 220.0, 200),   # ST, 8% loss
    ("BRK-B", "Financials",             20,  430.0, 445.0, 600),   # LT gain
    ("UNH", "Health Care",              12,  540.0, 520.0, 180),   # ST, 4% loss
    ("JPM", "Financials",               35,  195.0, 210.0, 700),   # LT gain
    ("JNJ", "Health Care",              25,  155.0, 152.0, 450),   # LT, tiny loss
    ("V", "Financials",                 18,  280.0, 295.0, 800),   # LT gain
    ("PG", "Consumer Staples",          22,  160.0, 168.0, 550),   # LT gain
    ("HD", "Consumer Discretionary",    15,  390.0, 370.0, 250),   # ST, 5% loss
    ("MA", "Financials",                12,  465.0, 455.0, 120),   # ST, 2% loss
    ("XOM", "Energy",                   40,  105.0,  98.0,  80),   # ST, 7% loss
    ("CVX", "Energy",                   30,  155.0, 148.0,  75),   # ST, 5% loss
    ("PFE", "Health Care",              60,   35.0,  28.0, 150),   # ST, 20% loss (harvestable)
    ("KO",  "Consumer Staples",         50,   63.0,  65.0, 900),   # LT gain
    ("DIS", "Communication Services",   20,  115.0,  95.0, 300),   # near-LT, 17% loss
]

# A couple of historical harvest transactions so the Reports tab has rows
HISTORICAL_HARVESTS = [
    # (symbol, shares, price, economic_loss, wash_disallowed, days_ago)
    ("INTC", 80, 32.0,  -480.0,  0.0,  120),
    ("T",    50, 17.0,  -250.0, 50.0,   90),   # $50 of disallowed loss
    ("F",   100, 11.5,  -300.0,  0.0,   60),
]


def _fmt(r):
    return f"{r['shares']:g}×{r['symbol']} @ ${r['current_price']:.2f}"


async def _wipe_portfolio(db, portfolio_id: int):
    """Remove positions/lots/transactions for a portfolio — used with --reset."""
    pos_rows = await db.execute(select(Position).where(Position.portfolio_id == portfolio_id))
    for p in pos_rows.scalars().all():
        await db.execute(delete(TaxLot).where(TaxLot.position_id == p.id))
    await db.execute(delete(Position).where(Position.portfolio_id == portfolio_id))
    await db.execute(delete(Transaction).where(Transaction.portfolio_id == portfolio_id))


async def _seed(email: str, password: str, name: str, reset: bool) -> dict:
    async with AsyncSessionLocal() as db:
        # Find or create the user
        existing_user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing_user and reset:
            # Delete portfolios → cascades to positions, transactions, exclusions
            portfolios = (await db.execute(
                select(Portfolio).join(Client).where(Client.advisor_id == existing_user.id)
            )).scalars().all()
            for p in portfolios:
                await _wipe_portfolio(db, p.id)
                await db.delete(p)
            # Delete client(s)
            clients = (await db.execute(
                select(Client).where(Client.advisor_id == existing_user.id)
            )).scalars().all()
            for c in clients:
                await db.delete(c)
            # Delete acknowledgements
            await db.execute(delete(Acknowledgement).where(Acknowledgement.user_id == existing_user.id))
            await db.commit()
            existing_user = None

        if existing_user:
            user = existing_user
            client = (await db.execute(
                select(Client).where(Client.advisor_id == user.id, Client.is_self == True)  # noqa: E712
            )).scalar_one_or_none()
            if not client:
                client = Client(advisor_id=user.id, name=name, email=email, is_self=True)
                db.add(client)
                await db.commit()
                await db.refresh(client)
        else:
            user, client = await create_individual_user(
                db, email=email, password=password, full_name=name,
            )
            user.email_verified = True   # Skip the verification step for demo users
            await db.commit()

        # Auto-accept all current ack versions so the agent isn't gated
        for doc_type, version in CURRENT_VERSIONS.items():
            existing_ack = (await db.execute(
                select(Acknowledgement).where(
                    Acknowledgement.user_id == user.id,
                    Acknowledgement.document_type == doc_type,
                )
            )).scalar_one_or_none()
            if existing_ack is None:
                db.add(Acknowledgement(
                    user_id=user.id, document_type=doc_type, version=version,
                    accepted_at=datetime.utcnow(),
                ))
        await db.commit()

        # Find or create the portfolio
        portfolio = (await db.execute(
            select(Portfolio).where(Portfolio.client_id == client.id).limit(1)
        )).scalar_one_or_none()
        initial = sum(s * c for _, _, s, c, _, _ in POSITIONS)
        if portfolio is None:
            portfolio = Portfolio(
                name="My Direct Index (Demo)",
                initial_value=round(initial, 2),
                cash=250.0,
                client_id=client.id,
            )
            db.add(portfolio)
            await db.commit()
            await db.refresh(portfolio)
        else:
            # In-place refresh — wipe old positions + lots + transactions
            await _wipe_portfolio(db, portfolio.id)
            portfolio.initial_value = round(initial, 2)
            portfolio.cash = 250.0
            await db.commit()
            await db.refresh(portfolio)

        now = datetime.utcnow()
        total_mv = 0.0
        for sym, sector, shares, avg_cost, price, days_ago in POSITIONS:
            pos = Position(
                portfolio_id=portfolio.id,
                symbol=sym, name=sym, sector=sector,
                shares=shares, avg_cost_basis=avg_cost,
                target_weight=1.0 / len(POSITIONS),
                is_active=True,
            )
            db.add(pos)
            await db.flush()
            db.add(TaxLot(
                position_id=pos.id,
                shares=shares, cost_basis=avg_cost,
                purchase_date=now - timedelta(days=days_ago),
                wash_sale_disallowed=0.0,
            ))
            total_mv += shares * price

        # Historical harvest transactions (closed lots with realized loss)
        for sym, shares, price, econ_loss, wash, days_ago in HISTORICAL_HARVESTS:
            cost = price + abs(econ_loss / shares)   # derive basis from the loss
            pos = Position(
                portfolio_id=portfolio.id,
                symbol=sym, name=sym, sector="Technology",
                shares=0.0, avg_cost_basis=cost,
                target_weight=0.0, is_active=False,
            )
            db.add(pos)
            await db.flush()
            sale_at = now - timedelta(days=days_ago)
            db.add(TaxLot(
                position_id=pos.id, shares=0.0, cost_basis=cost,
                purchase_date=sale_at - timedelta(days=45),
                sale_date=sale_at,
                proceeds=shares * price,
                realized_gain_loss=econ_loss,
                wash_sale_disallowed=wash,
            ))
            db.add(Transaction(
                portfolio_id=portfolio.id, symbol=sym,
                transaction_type="HARVEST",
                shares=shares, price=price, total_value=shares * price,
                timestamp=sale_at,
                notes=f"Historical harvest: ${econ_loss:,.2f} loss" + (f" (${wash:.2f} wash-sale disallowed)" if wash else ""),
            ))

        await db.commit()
        return {
            "user_id": user.id,
            "email": email,
            "password": password,
            "portfolio_id": portfolio.id,
            "positions_created": len(POSITIONS),
            "historical_harvests": len(HISTORICAL_HARVESTS),
            "total_market_value": round(total_mv, 2),
        }


def _parse():
    p = argparse.ArgumentParser(description="Seed a realistic demo portfolio.")
    p.add_argument("--email", default="demo@example.com")
    p.add_argument("--password", default="demo12345")
    p.add_argument("--name", default="Demo User")
    p.add_argument("--reset", action="store_true",
                   help="Drop existing portfolios for this user and recreate.")
    return p.parse_args()


async def _main():
    args = _parse()
    await init_db()   # safe no-op if tables exist
    summary = await _seed(args.email, args.password, args.name, args.reset)
    print("Seeded demo portfolio:")
    for k, v in summary.items():
        print(f"  {k:24s} {v}")
    print()
    print(f"Log in at /login with {summary['email']} / {summary['password']}")


if __name__ == "__main__":
    asyncio.run(_main())
