from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...database import get_db
from ...models.models import Portfolio
from ...services.audit import log_audit
from ...services.csv_importer import parse_lot_csv, import_lots_to_portfolio
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios/{portfolio_id}/import", tags=["import"])


@router.post("/lots")
async def import_lots(
    portfolio_id: int,
    request: Request,
    file: UploadFile = File(..., description="Schwab or Fidelity lot-level CSV export"),
    overwrite_existing: bool = Form(False),
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a Schwab or Fidelity cost-basis CSV to populate the tax-lot table.

    - Auto-detects broker format from column headers.
    - Appends new lots to existing positions (or replaces if overwrite_existing=true).
    - Returns a summary of imported lots per symbol.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "File must be a .csv")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10 MB guard
        raise HTTPException(413, "File too large (max 10 MB)")

    try:
        fmt, lots = parse_lot_csv(content)
    except ValueError as e:
        raise HTTPException(422, f"CSV parse error: {e}")

    if not lots:
        raise HTTPException(422, "No valid lot rows found in CSV")

    try:
        result = await import_lots_to_portfolio(
            db=db,
            portfolio_id=portfolio.id,
            lots=lots,
            overwrite_existing=overwrite_existing,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))

    await log_audit(
        db, event_type="LOTS_IMPORTED",
        user_id=current_user.id, portfolio_id=portfolio.id,
        object_type="portfolio", object_id=portfolio.id,
        details={
            "detected_format": fmt,
            "lot_count": len(lots),
            "symbols": sorted({l["symbol"] for l in lots})[:50],
            "overwrite_existing": overwrite_existing,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {
        "detected_format": fmt,
        **result,
    }


@router.post("/lots/preview")
async def preview_import(
    file: UploadFile = File(...),
    portfolio: Portfolio = Depends(assert_portfolio_access),
):
    """
    Parse a CSV and return the parsed rows without writing to the database.
    Use this to verify the import looks correct before committing.
    """
    content = await file.read()
    try:
        fmt, lots = parse_lot_csv(content)
    except ValueError as e:
        raise HTTPException(422, f"CSV parse error: {e}")

    return {
        "detected_format": fmt,
        "lot_count": len(lots),
        "symbols": sorted({l["symbol"] for l in lots}),
        "preview": [
            {
                "symbol": l["symbol"],
                "date_acquired": l["date_acquired"].strftime("%Y-%m-%d"),
                "shares": l["shares"],
                "cost_per_share": l["cost_per_share"],
                "total_cost": round(l["shares"] * l["cost_per_share"], 2),
            }
            for l in lots[:50]   # first 50 rows only
        ],
    }
