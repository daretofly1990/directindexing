"""
Position-scoped routes: Spec-ID sale of user-selected tax lots.

POST /api/portfolios/{portfolio_id}/positions/{position_id}/sell
  Close the given lot IDs at live (or override) price.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...database import get_db
from ...models.models import Portfolio, Position
from ...services.idempotency import cache_response, get_cached_response
from ...services.lot_engine import lot_engine
from ...services.sell_service import execute_spec_id_sale
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios/{portfolio_id}/positions", tags=["positions"])


class SpecIdSellRequest(BaseModel):
    lot_ids: list[int]
    override_price: float | None = None


@router.get("/{position_id}/lots")
async def get_position_lots(
    portfolio_id: int,
    position_id: int,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """Open lots for a single position, for the Spec-ID sell picker."""
    pos = await db.get(Position, position_id)
    if not pos or pos.portfolio_id != portfolio.id:
        raise HTTPException(404, "Position not found")
    lots = await lot_engine.get_open_lots(db, pos.id)
    now = datetime.utcnow()
    return {
        "symbol": pos.symbol,
        "shares": pos.shares,
        "avg_cost_basis": pos.avg_cost_basis,
        "lots": [
            {
                "lot_id": l.id,
                "shares": l.shares,
                "cost_basis": l.cost_basis,
                "purchase_date": l.purchase_date.isoformat(),
                "holding_days": (now - l.purchase_date).days,
                "is_long_term": (now - l.purchase_date).days >= 365,
            }
            for l in lots
        ],
    }


@router.post("/{position_id}/sell")
async def sell_position(
    portfolio_id: int,
    position_id: int,
    req: SpecIdSellRequest,
    request: Request,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Spec-ID sale: close the specific lots the user selected.

    Send `Idempotency-Key: <uuid>` header to make retries safe. The same key
    submitted twice returns the cached response instead of re-executing.
    """
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        cached = await get_cached_response(db, idem_key, current_user.id, "sell")
        if cached is not None:
            return cached
    try:
        result = await execute_spec_id_sale(
            db=db,
            portfolio_id=portfolio.id,
            position_id=position_id,
            lot_ids=req.lot_ids,
            override_price=req.override_price,
            user_id=current_user.id,
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as e:
        msg = str(e)
        if msg.startswith("SELL_CAP_EXCEEDED"):
            raise HTTPException(400, msg)
        raise HTTPException(404, msg)
    if idem_key:
        await cache_response(db, idem_key, current_user.id, "sell", result)
    return result
