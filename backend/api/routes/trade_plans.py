"""
TradePlan lifecycle + broker-specific CSV export + post-trade reconcile.

POST   /portfolios/{id}/trade-plans           — create from a draft_plan dict
GET    /portfolios/{id}/trade-plans           — list
GET    /portfolios/{id}/trade-plans/{plan_id} — detail
POST   /portfolios/{id}/trade-plans/{plan_id}/approve
POST   /portfolios/{id}/trade-plans/{plan_id}/cancel
POST   /portfolios/{id}/trade-plans/{plan_id}/mark-executed
GET    /portfolios/{id}/trade-plans/{plan_id}/export.csv?format=schwab|fidelity|generic
POST   /portfolios/{id}/trade-plans/{plan_id}/reconcile (multipart upload)
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...database import get_db
from ...models.models import Portfolio, TradePlan
from ...services import trade_plan_service
from ...services.idempotency import cache_response, get_cached_response
from ...services.kill_switch import assert_trading_enabled
from ...services.reconcile_diff import diff_plan_vs_csv, _sum_shares_by_symbol
from ...services.trade_export import EXPORTERS
from ...services.csv_importer import parse_lot_csv, import_lots_to_portfolio
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios/{portfolio_id}/trade-plans", tags=["trade-plans"])


class CreatePlanRequest(BaseModel):
    draft_plan: dict
    summary: str = ""
    recommendation_log_id: int | None = None


class ExecNotesRequest(BaseModel):
    notes: str = ""


@router.get("")
async def list_plans(
    portfolio_id: int,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    plans = await trade_plan_service.list_plans(db, portfolio.id)
    return [trade_plan_service.plan_to_dict(p) for p in plans]


@router.post("")
async def create_plan(
    portfolio_id: int,
    req: CreatePlanRequest,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        plan = await trade_plan_service.create_trade_plan(
            db, portfolio.id, req.draft_plan, current_user.id,
            summary=req.summary, recommendation_log_id=req.recommendation_log_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.refresh(plan, ["items"])
    return trade_plan_service.plan_to_dict(plan)


@router.get("/{plan_id}")
async def get_plan(
    portfolio_id: int, plan_id: int,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    plan = await db.get(TradePlan, plan_id)
    if not plan or plan.portfolio_id != portfolio.id:
        raise HTTPException(404, "Plan not found")
    await db.refresh(plan, ["items"])
    return trade_plan_service.plan_to_dict(plan)


@router.post("/{plan_id}/approve")
async def approve_plan(
    portfolio_id: int, plan_id: int,
    request: Request,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await assert_trading_enabled(db)
    idem_key = request.headers.get("Idempotency-Key")
    endpoint = f"plan_approve:{plan_id}"
    if idem_key:
        cached = await get_cached_response(db, idem_key, current_user.id, endpoint)
        if cached is not None:
            return cached
    try:
        plan = await trade_plan_service.approve_plan(db, plan_id, current_user.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if plan.portfolio_id != portfolio.id:
        raise HTTPException(404, "Plan not found")
    await db.refresh(plan, ["items"])
    result = trade_plan_service.plan_to_dict(plan)
    if idem_key:
        await cache_response(db, idem_key, current_user.id, endpoint, result)
    return result


@router.post("/{plan_id}/cancel")
async def cancel_plan(
    portfolio_id: int, plan_id: int,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        plan = await trade_plan_service.cancel_plan(db, plan_id, current_user.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if plan.portfolio_id != portfolio.id:
        raise HTTPException(404, "Plan not found")
    await db.refresh(plan, ["items"])
    return trade_plan_service.plan_to_dict(plan)


@router.post("/{plan_id}/mark-executed")
async def mark_executed(
    portfolio_id: int, plan_id: int,
    req: ExecNotesRequest,
    request: Request,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await assert_trading_enabled(db)
    idem_key = request.headers.get("Idempotency-Key")
    endpoint = f"plan_mark_executed:{plan_id}"
    if idem_key:
        cached = await get_cached_response(db, idem_key, current_user.id, endpoint)
        if cached is not None:
            return cached
    try:
        plan = await trade_plan_service.mark_executed(db, plan_id, current_user.id, notes=req.notes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if plan.portfolio_id != portfolio.id:
        raise HTTPException(404, "Plan not found")
    await db.refresh(plan, ["items"])
    result = trade_plan_service.plan_to_dict(plan)
    if idem_key:
        await cache_response(db, idem_key, current_user.id, endpoint, result)
    return result


@router.get("/{plan_id}/export.csv")
async def export_plan_csv(
    portfolio_id: int, plan_id: int,
    format: str = Query("generic", pattern="^(schwab|fidelity|generic)$"),
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    plan = await db.get(TradePlan, plan_id)
    if not plan or plan.portfolio_id != portfolio.id:
        raise HTTPException(404, "Plan not found")
    await db.refresh(plan, ["items"])
    csv_body = EXPORTERS[format](plan)
    filename = f"trade_plan_{plan.id}_{format}.csv"
    return StreamingResponse(
        iter([csv_body]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/{plan_id}/reconcile")
async def reconcile_fills(
    portfolio_id: int, plan_id: int,
    file: UploadFile = File(..., description="Fresh broker CSV after execution"),
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Post-execution reconcile: user re-uploads their lot CSV. We import the
    updated lots (overwrite=True), mark the plan as EXECUTED, and return
    symbols that changed vs. planned.
    """
    plan = await db.get(TradePlan, plan_id)
    if not plan or plan.portfolio_id != portfolio.id:
        raise HTTPException(404, "Plan not found")
    if plan.status not in ("APPROVED", "EXECUTED"):
        raise HTTPException(400, f"Plan must be APPROVED before reconcile (was {plan.status})")
    await db.refresh(plan, ["items"])

    content = await file.read()
    try:
        fmt, lots = parse_lot_csv(content)
    except ValueError as e:
        raise HTTPException(422, f"CSV parse error: {e}")

    # Snapshot pre-import share totals so we can diff post-import actuals.
    from sqlalchemy import select as _select
    from ...models.models import Position as _Position
    pre_result = await db.execute(
        _select(_Position.symbol, _Position.shares).where(
            _Position.portfolio_id == portfolio.id,
        )
    )
    pre_totals = {row[0].upper(): float(row[1] or 0) for row in pre_result.all()}
    post_totals = _sum_shares_by_symbol(lots)

    diff = diff_plan_vs_csv(plan.items, pre_totals, post_totals)

    try:
        result = await import_lots_to_portfolio(
            db=db, portfolio_id=portfolio.id, lots=lots, overwrite_existing=True,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))

    if plan.status == "APPROVED":
        try:
            reconcile_note = (
                f"Reconciled from {fmt} CSV — {result.get('imported_lots', 0)} lots. "
                f"Partial: {diff['summary']['any_partial']}, "
                f"Missed: {diff['summary']['any_missed']}, "
                f"Unexpected: {len(diff['unexpected_symbols'])}."
            )
            await trade_plan_service.mark_executed(
                db, plan.id, current_user.id, notes=reconcile_note,
            )
        except ValueError:
            pass

    return {
        "plan_id": plan.id,
        "detected_format": fmt,
        "diff": diff,
        **result,
    }
