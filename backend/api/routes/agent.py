"""
AI-powered TLH advisor endpoint + direct REST wrappers for each primitive.

POST /api/portfolios/{id}/harvest-agent
  Run the Claude reasoning loop and return a draft trade plan.

GET  /api/portfolios/{id}/tlh/losses
POST /api/portfolios/{id}/tlh/simulate
GET  /api/portfolios/{id}/tlh/wash-sale/{symbol}
POST /api/portfolios/{id}/tlh/replacement
POST /api/portfolios/{id}/tlh/draft
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...database import get_db
from ...models.models import Portfolio
from ...services import tlh_tools
from ...services.tlh_agent import run_tlh_agent, SYSTEM_PROMPT
from ...services.audit import log_recommendation
from ...services.ai_guardrails import (
    apply_guardrails,
    validate_draft_plan_schema,
    PROMPT_VERSION,
    MODEL_VERSION,
)
from ...services.disclosures import ADVISOR_DISCLOSURE
from ...services.reasoning_builder import enrich_draft_plan
from .acknowledgements import CURRENT_VERSIONS, user_has_accepted
from ..deps import assert_portfolio_access

router = APIRouter(prefix="/portfolios/{portfolio_id}", tags=["tlh-agent"])


# ---------------------------------------------------------------------------
# AI agent
# ---------------------------------------------------------------------------

class AgentRequest(BaseModel):
    instruction: str = "Identify the best tax-loss harvesting opportunities and draft a trade plan."
    tax_rate_short: float = 0.37
    tax_rate_long: float = 0.20
    max_iterations: int = 10


@router.post("/harvest-agent")
async def harvest_agent(
    portfolio_id: int,
    req: AgentRequest,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Run the Claude TLH reasoning loop.

    Claude autonomously calls find_losses → simulate_sale → check_wash_sale →
    propose_replacement → draft_trade_list, then returns a draft plan + summary.
    Requires ANTHROPIC_API_KEY in .env.

    For individual users, ADV Part 2A must be acknowledged before any
    personalized recommendation is generated (SEC rule).
    """
    if current_user.role == "individual":
        if not await user_has_accepted(db, current_user.id, "adv_part_2a"):
            raise HTTPException(
                403,
                "ADV Part 2A brochure must be acknowledged before using the advisor. "
                "POST /api/acknowledgements with document_type=adv_part_2a.",
            )

    try:
        result = await run_tlh_agent(
            db=db,
            portfolio_id=portfolio.id,
            user_instruction=req.instruction,
            tax_rate_short=req.tax_rate_short,
            tax_rate_long=req.tax_rate_long,
            max_iterations=req.max_iterations,
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(500, f"Agent error: {e}")

    # Guardrails: enforce wash-sale / substantially-identical / max-pct caps
    draft_plan = result.get("draft_plan")
    if draft_plan is not None:
        ok, errors = validate_draft_plan_schema(draft_plan)
        if not ok:
            result["schema_errors"] = errors
            draft_plan = None
            result["draft_plan"] = None
        else:
            draft_plan, guard_warnings = await apply_guardrails(
                db, portfolio.id, draft_plan,
            )
            # Attach per-lot citations + confidence + caveats for transparency
            draft_plan = await enrich_draft_plan(db, portfolio.id, draft_plan)
            result["draft_plan"] = draft_plan
            if guard_warnings:
                result["guardrail_warnings"] = guard_warnings

    # Persist an immutable recommendation log for compliance reconstruction
    reasoning_text = "\n".join(
        s.get("content", "") for s in result.get("reasoning_steps", []) if s.get("type") == "text"
    )
    tool_calls = [
        s for s in result.get("reasoning_steps", []) if s.get("type") == "tool_call"
    ]
    rec_log = await log_recommendation(
        db=db,
        user_id=current_user.id,
        portfolio_id=portfolio.id,
        prompt=req.instruction,
        reasoning=reasoning_text,
        tool_calls=tool_calls,
        draft_plan=draft_plan,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
        adv_version_acknowledged=CURRENT_VERSIONS.get("adv_part_2a"),
        demo_mode=bool(result.get("demo_mode")),
    )
    result["recommendation_log_id"] = rec_log.id
    result["disclosure"] = ADVISOR_DISCLOSURE
    return result


# ---------------------------------------------------------------------------
# Direct primitive endpoints
# ---------------------------------------------------------------------------

class SimulateSaleRequest(BaseModel):
    lot_ids: list[int]
    override_price: float | None = None


class ReplacementRequest(BaseModel):
    symbol: str
    avoid_symbols: list[str] = []
    sector: str | None = None


class HarvestItem(BaseModel):
    lot_ids: list[int]
    replacement_symbol: str = ""


class DraftPlanRequest(BaseModel):
    harvests: list[HarvestItem]
    tax_rate_short: float = 0.37
    tax_rate_long: float = 0.20


@router.get("/tlh/losses")
async def get_losses(
    portfolio_id: int,
    target_amount: float | None = None,
    min_loss_pct: float = 0.02,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """Identify positions with unrealized losses above the threshold."""
    return await tlh_tools.find_losses(
        db, portfolio.id,
        target_amount=target_amount,
        min_loss_pct=min_loss_pct,
    )


@router.post("/tlh/simulate")
async def simulate(
    portfolio_id: int,
    req: SimulateSaleRequest,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """Simulate selling specific lots (Spec-ID). Read-only — no trades executed."""
    result = await tlh_tools.simulate_sale(
        db, portfolio.id,
        lot_ids=req.lot_ids,
        override_price=req.override_price,
    )
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.get("/tlh/wash-sale/{symbol}")
async def wash_sale_check(
    portfolio_id: int,
    symbol: str,
    window_days: int = 30,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """Check wash-sale status for a symbol."""
    return await tlh_tools.check_wash_sale(
        db, portfolio.id, symbol, window_days=window_days
    )


@router.post("/tlh/replacement")
async def get_replacement(
    portfolio_id: int,
    req: ReplacementRequest,
    portfolio: Portfolio = Depends(assert_portfolio_access),
):
    """Propose replacement securities for a harvested position."""
    return await tlh_tools.propose_replacement(
        symbol=req.symbol,
        avoid_symbols=req.avoid_symbols,
        sector=req.sector,
    )


@router.post("/tlh/draft")
async def draft_plan(
    portfolio_id: int,
    req: DraftPlanRequest,
    portfolio: Portfolio = Depends(assert_portfolio_access),
    db: AsyncSession = Depends(get_db),
):
    """Compile a complete trade plan from a list of harvest decisions."""
    try:
        return await tlh_tools.draft_trade_list(
            db, portfolio.id,
            harvests=[h.model_dump() for h in req.harvests],
            tax_rate_short=req.tax_rate_short,
            tax_rate_long=req.tax_rate_long,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
