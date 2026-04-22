"""
Claude-powered tax-loss harvesting agent.

Uses the Anthropic Messages API with tool_use to orchestrate the five TLH
primitives (find_losses, simulate_sale, check_wash_sale, propose_replacement,
draft_trade_list) via a reasoning loop.

The loop runs entirely server-side. The caller gets back a structured result:
  - reasoning_steps: list of text + tool calls Claude made
  - draft_plan: the final draft_trade_list output (if Claude produced one)
  - summary: Claude's natural-language summary for the advisor
"""
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import tlh_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions for the Anthropic API
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "find_losses",
        "description": (
            "Find positions in the portfolio with unrealized losses suitable for tax-loss harvesting. "
            "Returns opportunities sorted by largest loss first, with per-lot detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_amount": {
                    "type": "number",
                    "description": "Stop when cumulative harvestable loss reaches this dollar amount (optional).",
                },
                "min_loss_pct": {
                    "type": "number",
                    "description": "Minimum position-level loss percentage to include, e.g. 0.02 = 2% (default 0.02).",
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of symbols to restrict the search to.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "simulate_sale",
        "description": (
            "Simulate selling specific tax lots (Spec-ID) and see the projected gain/loss, "
            "tax impact, and wash-sale risk. Does NOT execute any trades."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lot_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "IDs of the specific tax lots to sell.",
                },
                "override_price": {
                    "type": "number",
                    "description": "Use this price per share instead of fetching live market price (optional).",
                },
            },
            "required": ["lot_ids"],
        },
    },
    {
        "name": "check_wash_sale",
        "description": (
            "Check whether selling a symbol now would trigger a wash-sale, and whether "
            "the portfolio is still in the post-sale window from a recent harvest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol to check.",
                },
                "window_days": {
                    "type": "integer",
                    "description": "Wash-sale window in days (default 30).",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "propose_replacement",
        "description": (
            "Propose replacement securities that maintain similar market exposure "
            "without triggering the substantially-identical security rule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "The symbol being harvested.",
                },
                "avoid_symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Symbols already in the portfolio or recently sold (to avoid).",
                },
                "sector": {
                    "type": "string",
                    "description": "GICS sector of the security, e.g. 'Information Technology'.",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "draft_trade_list",
        "description": (
            "Compile the complete harvest plan into a structured sell + buy list "
            "with compliance checks. This is the final step before presenting to the user for approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "harvests": {
                    "type": "array",
                    "description": "List of harvest items, each with lot_ids and replacement_symbol.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "lot_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "Specific lot IDs to sell.",
                            },
                            "replacement_symbol": {
                                "type": "string",
                                "description": "Symbol to buy as replacement.",
                            },
                        },
                        "required": ["lot_ids"],
                    },
                },
                "tax_rate_short": {
                    "type": "number",
                    "description": "Client's short-term capital gains rate (default 0.37).",
                },
                "tax_rate_long": {
                    "type": "number",
                    "description": "Client's long-term capital gains rate (default 0.20).",
                },
            },
            "required": ["harvests"],
        },
    },
]

SYSTEM_PROMPT = """You are an expert tax-loss harvesting advisor with direct access to a client's portfolio.

Your goal: analyse the portfolio, identify the best tax-loss harvesting opportunities, verify wash-sale compliance, propose suitable replacements, and produce a concrete trade plan for the advisor to review and approve.

Process:
1. Call find_losses to identify positions with unrealized losses. Start with the defaults unless the user specified a target amount or specific symbols.
2. For the top opportunities, call simulate_sale with the loss lots to confirm the exact tax impact.
3. Call check_wash_sale for each symbol you plan to harvest, to verify compliance.
4. Call propose_replacement for each harvested symbol to find a suitable alternative.
5. Call draft_trade_list once with all harvests compiled into a single plan.
6. Summarise the plan clearly: what is being sold, why, what replaces it, and the estimated tax savings.

Rules:
- Never suggest a replacement that is substantially identical to the sold security.
- Never execute trades — your output is a draft plan for human review.
- Prefer long-term losses (taxed at 20%) if both ST and LT losses are available, unless the user specifies otherwise.
- Be specific with dollar amounts and percentages.
- If a wash-sale risk exists, explain it clearly and suggest how to avoid it.
"""


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

async def _dispatch(
    db: AsyncSession,
    portfolio_id: int,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict:
    try:
        if tool_name == "find_losses":
            return await tlh_tools.find_losses(db, portfolio_id, **tool_input)
        if tool_name == "simulate_sale":
            return await tlh_tools.simulate_sale(db, portfolio_id, **tool_input)
        if tool_name == "check_wash_sale":
            return await tlh_tools.check_wash_sale(db, portfolio_id, **tool_input)
        if tool_name == "propose_replacement":
            return await tlh_tools.propose_replacement(**tool_input)
        if tool_name == "draft_trade_list":
            return await tlh_tools.draft_trade_list(db, portfolio_id, **tool_input)
        return {"error": f"Unknown tool: {tool_name}"}
    except Exception as exc:
        logger.error("Tool %s failed: %s", tool_name, exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def _run_demo_agent(
    db: AsyncSession,
    portfolio_id: int,
    user_instruction: str,
    tax_rate_short: float,
    tax_rate_long: float,
) -> dict:
    """
    Demo mode — runs the real TLH primitives but skips the Anthropic API call.
    Simulates the reasoning steps Claude would take, using live portfolio data.
    """
    reasoning_steps = []

    reasoning_steps.append({
        "type": "text",
        "content": f"Analysing portfolio {portfolio_id} for tax-loss harvesting opportunities...",
    })

    # Step 1: find losses
    losses = await tlh_tools.find_losses(db, portfolio_id, min_loss_pct=0.02)
    reasoning_steps.append({
        "type": "tool_call",
        "tool": "find_losses",
        "input": {"min_loss_pct": 0.02},
        "result": losses,
    })

    opportunities = losses.get("opportunities", [])
    if not opportunities:
        return {
            "portfolio_id": portfolio_id,
            "draft_plan": None,
            "summary": "No positions currently meet the minimum loss threshold for harvesting. No action recommended at this time.",
            "reasoning_steps": reasoning_steps,
            "iterations": 1,
            "status": "incomplete",
            "demo_mode": True,
        }

    reasoning_steps.append({
        "type": "text",
        "content": (
            f"Found {len(opportunities)} position(s) with harvestable losses totalling "
            f"${abs(losses['total_harvestable_loss']):,.2f}. "
            "Checking wash-sale status and simulating top candidates..."
        ),
    })

    harvests = []
    for opp in opportunities[:3]:  # top 3 by loss size
        symbol = opp["symbol"]
        loss_lots = [l for l in opp["lots"] if l["unrealized_gl"] < 0]
        if not loss_lots:
            continue
        lot_ids = [l["lot_id"] for l in loss_lots]

        # Step 2: simulate sale
        sim = await tlh_tools.simulate_sale(db, portfolio_id, lot_ids)
        reasoning_steps.append({
            "type": "tool_call",
            "tool": "simulate_sale",
            "input": {"lot_ids": lot_ids},
            "result": sim,
        })

        # Step 3: wash-sale check
        ws = await tlh_tools.check_wash_sale(db, portfolio_id, symbol)
        reasoning_steps.append({
            "type": "tool_call",
            "tool": "check_wash_sale",
            "input": {"symbol": symbol},
            "result": ws,
        })

        if not ws["safe_to_sell"]:
            reasoning_steps.append({
                "type": "text",
                "content": f"Skipping {symbol} — wash-sale rule applies (purchased within last 30 days).",
            })
            continue

        # Step 4: replacement
        repl = await tlh_tools.propose_replacement(symbol, sector=opp.get("sector"))
        reasoning_steps.append({
            "type": "tool_call",
            "tool": "propose_replacement",
            "input": {"symbol": symbol, "sector": opp.get("sector")},
            "result": repl,
        })

        replacement_symbol = repl["candidates"][0]["symbol"] if repl["candidates"] else ""
        harvests.append({"lot_ids": lot_ids, "replacement_symbol": replacement_symbol})

    if not harvests:
        return {
            "portfolio_id": portfolio_id,
            "draft_plan": None,
            "summary": "All loss positions are currently blocked by wash-sale rules. No harvesting recommended.",
            "reasoning_steps": reasoning_steps,
            "iterations": len(reasoning_steps),
            "status": "incomplete",
            "demo_mode": True,
        }

    # Step 5: draft trade list
    draft = await tlh_tools.draft_trade_list(
        db, portfolio_id, harvests,
        tax_rate_short=tax_rate_short,
        tax_rate_long=tax_rate_long,
    )
    reasoning_steps.append({
        "type": "tool_call",
        "tool": "draft_trade_list",
        "input": {"harvests": harvests},
        "result": draft,
    })

    savings = draft["summary"]["estimated_tax_savings"]
    st_loss = draft["summary"]["total_short_term_loss"]
    lt_loss = draft["summary"]["total_long_term_loss"]
    sells = len(draft["sells"])
    buys = len(draft["buys"])

    summary = (
        f"I identified {len(harvests)} harvesting opportunit{'y' if len(harvests)==1 else 'ies'} "
        f"across your portfolio.\n\n"
        f"**Plan:** {sells} sell order{'s' if sells!=1 else ''} + {buys} replacement buy{'s' if buys!=1 else ''}.\n"
        f"- Short-term losses: ${abs(st_loss):,.2f} (saves ~${abs(st_loss)*tax_rate_short:,.2f} in taxes)\n"
        f"- Long-term losses: ${abs(lt_loss):,.2f} (saves ~${abs(lt_loss)*tax_rate_long:,.2f} in taxes)\n"
        f"- **Estimated total tax savings: ${savings:,.2f}**\n\n"
        f"All replacements maintain your sector exposure without triggering the substantially-identical "
        f"security rule. The plan is ready for your review — no trades have been executed."
    )
    if draft["compliance"]["has_warnings"]:
        summary += "\n\n⚠️ Review compliance warnings before proceeding."

    reasoning_steps.append({"type": "text", "content": summary})

    return {
        "portfolio_id": portfolio_id,
        "draft_plan": draft,
        "summary": summary,
        "reasoning_steps": reasoning_steps,
        "iterations": len(reasoning_steps),
        "status": "draft",
        "demo_mode": True,
    }


async def run_tlh_agent(
    db: AsyncSession,
    portfolio_id: int,
    user_instruction: str,
    tax_rate_short: float = 0.37,
    tax_rate_long: float = 0.20,
    max_iterations: int = 10,
    model: str | None = None,
) -> dict:
    """
    Run the Claude TLH reasoning loop for a given portfolio.

    `model` defaults to settings.CLAUDE_MODEL_DEFAULT when not passed — the
    agent route resolves it per-user via billing_service.get_claude_model_for_user
    so premium subscribers get opus while everyone else gets the default
    (haiku after launch).

    Returns dict with reasoning_steps, draft_plan, summary, iterations, and
    the `model` actually used — propagated to RecommendationLog for audit.
    """
    resolved_model = model or settings.CLAUDE_MODEL_DEFAULT
    if not settings.ANTHROPIC_API_KEY:
        r = await _run_demo_agent(db, portfolio_id, user_instruction, tax_rate_short, tax_rate_long)
        r["model"] = "demo"
        return r

    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    system = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Portfolio ID: {portfolio_id}\n"
        f"Client tax rates: short-term {tax_rate_short:.0%}, long-term {tax_rate_long:.0%}"
    )

    messages: list[dict] = [{"role": "user", "content": user_instruction}]
    reasoning_steps: list[dict] = []
    draft_plan: dict | None = None
    iterations = 0

    try:
        while iterations < max_iterations:
            iterations += 1
            response = await client.messages.create(
                model=resolved_model,
                max_tokens=4096,
                system=system,
                tools=TOOLS,  # type: ignore[arg-type]
                messages=messages,
            )

            # Collect text output
            text_parts = [b.text for b in response.content if b.type == "text"]
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if text_parts:
                reasoning_steps.append({"type": "text", "content": "\n".join(text_parts)})

            if response.stop_reason == "end_turn" or not tool_uses:
                break

            # Execute all tool calls in this turn
            tool_results = []
            for tu in tool_uses:
                result = await _dispatch(db, portfolio_id, tu.name, tu.input)
                reasoning_steps.append({
                    "type": "tool_call",
                    "tool": tu.name,
                    "input": tu.input,
                    "result": result,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result),
                })

                # Capture draft plan when produced
                if tu.name == "draft_trade_list" and "error" not in result:
                    draft_plan = result

            # Feed results back
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        # Bad API key — again, OUR key, not the user's.
        logger.warning("Anthropic auth failed, falling back to demo: %s", exc)
        result = await _run_demo_agent(db, portfolio_id, user_instruction, tax_rate_short, tax_rate_long)
        result["model"] = "demo"
        result["attempted_model"] = resolved_model
        result["fallback_reason"] = "anthropic_auth_failed"
        result["fallback_message"] = (
            "The AI advisor is temporarily unavailable. The analysis below still "
            "uses the same tax-loss harvesting engine on your real positions."
        )
        result["fallback_admin_detail"] = f"Anthropic auth failed: {str(exc)[:200]}"
        return result
    except anthropic.BadRequestError as exc:
        # Most common: insufficient credits on OUR (the operator's) account —
        # users don't have Anthropic accounts. Show end-users a neutral message;
        # the admin sees the real diagnostic in `fallback_admin_detail`.
        detail = str(exc)
        logger.warning("Anthropic BadRequestError, falling back to demo: %s", detail[:300])
        result = await _run_demo_agent(db, portfolio_id, user_instruction, tax_rate_short, tax_rate_long)
        result["model"] = "demo"
        result["attempted_model"] = resolved_model
        result["fallback_reason"] = "anthropic_api_error"
        result["fallback_message"] = (
            "The AI advisor is temporarily unavailable. The analysis below "
            "comes from the same tax-loss harvesting engine using a simpler "
            "decision logic — your numbers are real, just not Claude's reasoning."
        )
        # Admin-only detail the frontend can show conditionally
        if "credit balance is too low" in detail.lower():
            result["fallback_admin_detail"] = (
                "Anthropic credit balance exhausted. Top up at "
                "console.anthropic.com → Plans & Billing."
            )
        elif "model" in detail.lower() and "not found" in detail.lower():
            result["fallback_admin_detail"] = (
                "Configured Claude model not available on this Anthropic account."
            )
        else:
            result["fallback_admin_detail"] = detail[:300]
        return result
    except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
        # Transient upstream failure — degrade to demo instead of 500
        logger.warning("Anthropic API unavailable, falling back to demo: %s", exc)
        result = await _run_demo_agent(db, portfolio_id, user_instruction, tax_rate_short, tax_rate_long)
        result["model"] = "demo"
        result["attempted_model"] = resolved_model
        result["fallback_reason"] = "anthropic_unavailable"
        result["fallback_message"] = (
            "The AI advisor is temporarily overloaded. The analysis below uses "
            "the same engine with a simpler decision path — try again in a few minutes for the full Claude reasoning."
        )
        result["fallback_admin_detail"] = f"Anthropic {type(exc).__name__}: {str(exc)[:200]}"
        return result

    summary = "\n".join(
        step["content"] for step in reasoning_steps if step["type"] == "text"
    )

    return {
        "portfolio_id": portfolio_id,
        "draft_plan": draft_plan,
        "summary": summary,
        "reasoning_steps": reasoning_steps,
        "iterations": iterations,
        "status": "draft" if draft_plan else "incomplete",
        "model": resolved_model,
    }
