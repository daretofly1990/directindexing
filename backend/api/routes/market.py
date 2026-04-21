from fastapi import APIRouter
from ...services.finnhub_client import finnhub_client
from ...services.sp500_data import SP500_CONSTITUENTS

router = APIRouter(prefix="/market", tags=["market"])

@router.get("/quote/{symbol}")
async def get_quote(symbol: str):
    return await finnhub_client.get_quote(symbol.upper())

@router.get("/constituents")
async def get_constituents():
    return SP500_CONSTITUENTS

@router.get("/sectors")
async def get_sectors():
    return sorted(set(c["sector"] for c in SP500_CONSTITUENTS))
