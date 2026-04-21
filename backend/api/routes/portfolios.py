import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from ...database import get_db
from ...auth import get_current_user
from ...models.models import Portfolio, TaxLot, Position
from ...services.audit import log_audit
from ...services.portfolio_service import portfolio_service
from ...services.lot_engine import lot_engine
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


class CreatePortfolioRequest(BaseModel):
    name: str
    initial_value: float
    client_id: int | None = None


class ConstructPortfolioRequest(BaseModel):
    excluded_sectors: list[str] = []
    excluded_symbols: list[str] = []


@router.get("")
async def list_portfolios(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    advisor_id = None if current_user.role == "admin" else current_user.id
    return await portfolio_service.list_portfolios(db, advisor_id=advisor_id)


@router.post("")
async def create_portfolio(
    req: CreatePortfolioRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    client_id = req.client_id
    # For individual users: silently pin the new portfolio to their self-client
    if current_user.role == "individual":
        from ...models.models import Client
        r = await db.execute(
            select(Client).where(
                Client.advisor_id == current_user.id,
                Client.is_self == True,  # noqa: E712
            )
        )
        self_client = r.scalar_one_or_none()
        if not self_client:
            raise HTTPException(400, "Individual user missing self-client")
        client_id = self_client.id
    p = await portfolio_service.create_portfolio(
        db, req.name, req.initial_value, client_id=client_id
    )
    return {"id": p.id, "name": p.name, "initial_value": p.initial_value, "cash": p.cash}


@router.get("/{portfolio_id}")
async def get_portfolio(
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await portfolio_service.get_portfolio_with_prices(db, portfolio.id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/{portfolio_id}/construct")
async def construct_portfolio(
    req: ConstructPortfolioRequest,
    request: Request,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await portfolio_service.construct_portfolio(
            db, portfolio.id, req.excluded_sectors, req.excluded_symbols
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    await log_audit(
        db, event_type="PORTFOLIO_CONSTRUCTED",
        user_id=current_user.id, portfolio_id=portfolio.id,
        object_type="portfolio", object_id=portfolio.id,
        details={
            "positions_created": result.get("positions_created"),
            "total_invested": result.get("total_invested"),
            "excluded_sectors": req.excluded_sectors,
            "excluded_symbols": req.excluded_symbols,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return result


@router.get("/{portfolio_id}/sectors")
async def get_sectors(
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    return await portfolio_service.get_sector_allocation(db, portfolio.id)


@router.get("/{portfolio_id}/transactions")
async def get_transactions(
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    return await portfolio_service.get_transactions(db, portfolio.id)


@router.get("/{portfolio_id}/tax-lots")
async def get_open_lots(
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """All open tax lots for active positions, with holding-period metadata."""
    return await lot_engine.get_open_lot_detail(db, portfolio.id)


@router.get("/{portfolio_id}/realized-gains")
async def get_realized_gains(
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """Realized gain/loss summary split by short-term vs. long-term, with wash-sale amounts."""
    return await lot_engine.get_realized_gain_summary(db, portfolio.id)


@router.get("/{portfolio_id}/tax-report.csv")
async def get_tax_report_csv(
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """Schedule D-format CSV: one row per closed tax lot."""
    result = await db.execute(
        select(TaxLot, Position.symbol)
        .join(Position, TaxLot.position_id == Position.id)
        .where(
            Position.portfolio_id == portfolio.id,
            TaxLot.sale_date.isnot(None),
        )
        .order_by(TaxLot.sale_date)
    )
    rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Description",
        "Date Acquired",
        "Date Sold",
        "Proceeds",
        "Cost Basis",
        "Wash Sale Disallowed",
        "Gain/Loss",
        "Term",
    ])

    for lot, symbol in rows:
        holding_days = (lot.sale_date - lot.purchase_date).days
        term = "Long-term" if holding_days >= 365 else "Short-term"
        cost_basis_total = round(lot.cost_basis * lot.shares, 2)
        description = f"{lot.shares:g} shares {symbol}"
        writer.writerow([
            description,
            lot.purchase_date.strftime("%m/%d/%Y"),
            lot.sale_date.strftime("%m/%d/%Y"),
            round(lot.proceeds or 0.0, 2),
            cost_basis_total,
            round(lot.wash_sale_disallowed or 0.0, 2),
            round(lot.realized_gain_loss or 0.0, 2),
            term,
        ])

    output.seek(0)
    filename = f"portfolio_{portfolio.id}_tax_report.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
