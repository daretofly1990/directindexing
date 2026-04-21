from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...database import get_db
from ...models.models import Portfolio
from ...services.audit import log_audit
from ...services.idempotency import cache_response, get_cached_response
from ...services.kill_switch import assert_trading_enabled
from ...services.rebalancing_service import rebalancing_service
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios/{portfolio_id}/rebalancing", tags=["rebalancing"])


@router.get("")
async def get_recommendations(
    portfolio_id: int,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    return await rebalancing_service.get_rebalancing_recommendations(db, portfolio_id)


@router.post("/execute")
async def execute(
    portfolio_id: int,
    request: Request,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await assert_trading_enabled(db)
    idem_key = request.headers.get("Idempotency-Key")
    endpoint = f"rebalance:{portfolio.id}"
    if idem_key:
        cached = await get_cached_response(db, idem_key, current_user.id, endpoint)
        if cached is not None:
            return cached
    result = await rebalancing_service.execute_rebalancing(db, portfolio_id)
    await log_audit(
        db, event_type="REBALANCE_EXECUTED",
        user_id=current_user.id, portfolio_id=portfolio.id,
        object_type="portfolio", object_id=portfolio.id,
        details={
            "trades_executed": result.get("trades_executed"),
            "new_cash_balance": result.get("new_cash_balance"),
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    if idem_key:
        await cache_response(db, idem_key, current_user.id, endpoint, result)
    return result
