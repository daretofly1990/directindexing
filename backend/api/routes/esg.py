from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from ...database import get_db
from ...models.models import Portfolio
from ...services.esg_service import esg_service
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios/{portfolio_id}/esg", tags=["esg"])


class AddExclusionRequest(BaseModel):
    exclusion_type: str
    value: str
    reason: str = None


@router.get("")
async def get_analysis(
    portfolio_id: int,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    return await esg_service.get_portfolio_esg_analysis(db, portfolio_id)


@router.get("/exclusions")
async def get_exclusions(
    portfolio_id: int,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    return await esg_service.get_exclusions(db, portfolio_id)


@router.post("/exclusions")
async def add_exclusion(
    portfolio_id: int,
    req: AddExclusionRequest,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    return await esg_service.add_exclusion(db, portfolio_id, req.exclusion_type, req.value, req.reason)


@router.delete("/exclusions/{exclusion_id}")
async def remove_exclusion(
    portfolio_id: int,
    exclusion_id: int,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    try:
        await esg_service.remove_exclusion(db, portfolio_id, exclusion_id)
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(404, str(e))
