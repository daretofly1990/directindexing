from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...database import get_db
from ...models.models import Portfolio
from ...services.audit import log_audit
from ...services.idempotency import cache_response, get_cached_response
from ...services.kill_switch import assert_trading_enabled
from ...services.tax_loss_service import tax_loss_service
from ...services.lot_engine import LotSelectionMethod
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios/{portfolio_id}/tax-loss", tags=["tax-loss"])


@router.get("")
async def get_opportunities(
    portfolio_id: int,
    method: LotSelectionMethod = Query(LotSelectionMethod.HIFO, description="Lot selection method"),
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    return await tax_loss_service.get_tax_loss_opportunities(db, portfolio_id, method)


@router.post("/{position_id}/harvest")
async def harvest(
    portfolio_id: int,
    position_id: int,
    request: Request,
    method: LotSelectionMethod = Query(LotSelectionMethod.HIFO, description="Lot selection method"),
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await assert_trading_enabled(db)
    idem_key = request.headers.get("Idempotency-Key")
    endpoint = f"harvest:{position_id}"
    if idem_key:
        cached = await get_cached_response(db, idem_key, current_user.id, endpoint)
        if cached is not None:
            return cached
    try:
        result = await tax_loss_service.execute_harvest(db, portfolio_id, position_id, method)
    except ValueError as e:
        raise HTTPException(404, str(e))
    await log_audit(
        db, event_type="HARVEST_EXECUTED",
        user_id=current_user.id, portfolio_id=portfolio_id,
        object_type="position", object_id=position_id,
        details={
            "symbol": result.get("symbol"), "proceeds": result.get("proceeds"),
            "economic_gain_loss": result.get("economic_gain_loss"),
            "wash_sale_disallowed": result.get("wash_sale_disallowed"),
            "method": str(method),
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    if idem_key:
        await cache_response(db, idem_key, current_user.id, endpoint, result)
    return result
