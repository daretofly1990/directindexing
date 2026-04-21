from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import date

from ...database import get_db
from ...models.models import Position, Portfolio
from ...services.backtest_service import backtest_service
from ...services.disclosures import BACKTEST_DISCLOSURE
from ...services.sp500_data import SP500_CONSTITUENTS, NASDAQ100_CONSTITUENTS, INDEX_MAP
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios/{portfolio_id}/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    start_date: date
    end_date: date
    initial_investment: float = 100_000.0
    simulate_tlh: bool = True
    tax_rate: float = 0.20
    tlh_threshold: float = 0.05
    index: str = "sp500"


@router.post("")
async def run_backtest(
    portfolio_id: int,
    req: BacktestRequest,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    if req.end_date <= req.start_date:
        raise HTTPException(400, "end_date must be after start_date")

    index_cfg = INDEX_MAP.get(req.index, INDEX_MAP["sp500"])
    constituents = index_cfg["constituents"]

    result = await db.execute(
        select(Position).where(
            Position.portfolio_id == portfolio_id,
            Position.is_active == True,
        )
    )
    positions = result.scalars().all()

    if positions and req.index == "sp500":
        symbols = [p.symbol for p in positions]
        weights = {p.symbol: p.target_weight for p in positions}
    else:
        symbols = [c["symbol"] for c in constituents]
        weights = {c["symbol"]: c["weight"] for c in constituents}

    try:
        result = await backtest_service.run_backtest(
            symbols=symbols,
            weights=weights,
            start_date=req.start_date,
            end_date=req.end_date,
            initial_investment=req.initial_investment,
            simulate_tlh=req.simulate_tlh,
            tax_rate=req.tax_rate,
            tlh_threshold=req.tlh_threshold,
            index=req.index,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Rule 206(4)-1 requires hypothetical performance carry a disclosure
    if isinstance(result, dict):
        result["disclosure"] = BACKTEST_DISCLOSURE
        result["performance_type"] = "hypothetical"
    return result
