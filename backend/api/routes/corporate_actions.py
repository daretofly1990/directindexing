from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...auth import require_admin, get_current_user
from ...services.audit import log_audit
from ...services.corporate_action_service import (
    process_split, process_delisting, process_spinoff, process_merger_cash,
)
from ...services.ticker_change_service import process_ticker_change

router = APIRouter(prefix="/admin/corporate-actions", tags=["admin"])


class SplitRequest(BaseModel):
    symbol: str
    old_rate: float
    new_rate: float
    ex_date: str | None = None
    notes: str | None = None


class DelistRequest(BaseModel):
    symbol: str
    ex_date: str | None = None
    notes: str | None = None


class SpinoffRequest(BaseModel):
    parent_symbol: str
    spin_symbol: str
    shares_per_parent: float
    basis_allocation_parent_pct: float  # 0..1
    ex_date: str | None = None
    notes: str | None = None


class MergerCashRequest(BaseModel):
    symbol: str
    cash_per_share: float
    ex_date: str | None = None
    notes: str | None = None


class TickerChangeRequest(BaseModel):
    old_symbol: str
    new_symbol: str
    ex_date: str | None = None
    notes: str | None = None


def _parse_date(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


async def _audit_corp(
    db, current_user, request: Request, action_type: str, details: dict,
):
    await log_audit(
        db, event_type=f"CORP_ACTION_{action_type.upper()}",
        user_id=current_user.id,
        object_type="corp_action", details=details,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()


@router.post("/process")
async def apply_split(
    req: SplitRequest,
    request: Request,
    current_user=Depends(get_current_user),
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually apply a stock split or reverse-split to all active positions."""
    try:
        result = await process_split(
            db, req.symbol.upper(), req.old_rate, req.new_rate,
            _parse_date(req.ex_date), req.notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit_corp(db, current_user, request, "split", result)
    return result


@router.post("/delist")
async def apply_delist(
    req: DelistRequest,
    request: Request,
    current_user=Depends(get_current_user),
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await process_delisting(
            db, req.symbol.upper(), _parse_date(req.ex_date), req.notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit_corp(db, current_user, request, "delist", result)
    return result


@router.post("/spinoff")
async def apply_spinoff(
    req: SpinoffRequest,
    request: Request,
    current_user=Depends(get_current_user),
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await process_spinoff(
            db, req.parent_symbol.upper(), req.spin_symbol.upper(),
            req.shares_per_parent, req.basis_allocation_parent_pct,
            _parse_date(req.ex_date), req.notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit_corp(db, current_user, request, "spinoff", result)
    return result


@router.post("/merger-cash")
async def apply_merger_cash(
    req: MergerCashRequest,
    request: Request,
    current_user=Depends(get_current_user),
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await process_merger_cash(
            db, req.symbol.upper(), req.cash_per_share,
            _parse_date(req.ex_date), req.notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit_corp(db, current_user, request, "merger_cash", result)
    return result


@router.post("/ticker-change")
async def apply_ticker_change(
    req: TickerChangeRequest,
    request: Request,
    current_user=Depends(get_current_user),
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Rename a symbol across all active positions (e.g. FB -> META).
    Lots, cost basis, purchase date, and wash-sale state are preserved.
    """
    try:
        result = await process_ticker_change(
            db,
            old_symbol=req.old_symbol.upper(),
            new_symbol=req.new_symbol.upper(),
            ex_date=_parse_date(req.ex_date),
            notes=req.notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit_corp(db, current_user, request, "ticker_change", result)
    return result
