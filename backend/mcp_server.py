"""
MCP server — exposes the five TLH primitives as MCP tools.

Run with:
    python -m backend.mcp_server

Claude Desktop / Cursor config (claude_desktop_config.json):
{
  "mcpServers": {
    "direct-indexing-tlh": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "/path/to/direct-indexing"
    }
  }
}

Each tool requires a portfolio_id argument so the MCP client can target a
specific portfolio without a separate session concept.
"""
import asyncio
import json
import logging
import os
import sys

# Add project root to path when run as __main__
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from backend.database import AsyncSessionLocal, init_db
from backend.services import tlh_tools

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger(__name__)

server = Server("direct-indexing-tlh")


# ---------------------------------------------------------------------------
# Tool list
# ---------------------------------------------------------------------------

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="find_losses",
            description=(
                "Find positions with unrealized losses suitable for tax-loss harvesting. "
                "Returns opportunities sorted by largest loss first, with per-lot detail."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "portfolio_id": {"type": "integer", "description": "Portfolio ID to analyse."},
                    "target_amount": {"type": "number", "description": "Stop at this $ of losses (optional)."},
                    "min_loss_pct": {"type": "number", "description": "Min loss % threshold (default 0.02)."},
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "Symbols to check (optional)."},
                },
                "required": ["portfolio_id"],
            },
        ),
        types.Tool(
            name="simulate_sale",
            description=(
                "Simulate selling specific tax lots (Spec-ID) and see projected gain/loss, "
                "tax impact, and wash-sale risk. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "portfolio_id": {"type": "integer"},
                    "lot_ids": {"type": "array", "items": {"type": "integer"}, "description": "Lot IDs to sell."},
                    "override_price": {"type": "number", "description": "Use this price instead of live market (optional)."},
                },
                "required": ["portfolio_id", "lot_ids"],
            },
        ),
        types.Tool(
            name="check_wash_sale",
            description="Check wash-sale status: safe to sell? Safe to repurchase? Earliest repurchase date?",
            inputSchema={
                "type": "object",
                "properties": {
                    "portfolio_id": {"type": "integer"},
                    "symbol": {"type": "string"},
                    "window_days": {"type": "integer", "description": "Wash-sale window (default 30)."},
                },
                "required": ["portfolio_id", "symbol"],
            },
        ),
        types.Tool(
            name="propose_replacement",
            description=(
                "Propose replacement securities that maintain market exposure "
                "without triggering the substantially-identical security rule."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol being harvested."},
                    "avoid_symbols": {"type": "array", "items": {"type": "string"}, "description": "Symbols to avoid."},
                    "sector": {"type": "string", "description": "GICS sector of the security."},
                },
                "required": ["symbol"],
            },
        ),
        types.Tool(
            name="draft_trade_list",
            description=(
                "Compile a complete sell+buy plan with compliance checks. "
                "Call this last after confirming losses and replacements."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "portfolio_id": {"type": "integer"},
                    "harvests": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "lot_ids": {"type": "array", "items": {"type": "integer"}},
                                "replacement_symbol": {"type": "string"},
                            },
                            "required": ["lot_ids"],
                        },
                    },
                    "tax_rate_short": {"type": "number", "description": "Short-term rate (default 0.37)."},
                    "tax_rate_long": {"type": "number", "description": "Long-term rate (default 0.20)."},
                },
                "required": ["portfolio_id", "harvests"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

@server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict,
) -> list[types.TextContent]:
    portfolio_id = arguments.get("portfolio_id")

    async with AsyncSessionLocal() as db:
        try:
            if name == "find_losses":
                result = await tlh_tools.find_losses(
                    db, portfolio_id,
                    target_amount=arguments.get("target_amount"),
                    min_loss_pct=arguments.get("min_loss_pct", 0.02),
                    symbols=arguments.get("symbols"),
                )
            elif name == "simulate_sale":
                result = await tlh_tools.simulate_sale(
                    db, portfolio_id,
                    lot_ids=arguments["lot_ids"],
                    override_price=arguments.get("override_price"),
                )
            elif name == "check_wash_sale":
                result = await tlh_tools.check_wash_sale(
                    db, portfolio_id,
                    symbol=arguments["symbol"],
                    window_days=arguments.get("window_days", 30),
                )
            elif name == "propose_replacement":
                result = await tlh_tools.propose_replacement(
                    symbol=arguments["symbol"],
                    avoid_symbols=arguments.get("avoid_symbols"),
                    sector=arguments.get("sector"),
                )
            elif name == "draft_trade_list":
                result = await tlh_tools.draft_trade_list(
                    db, portfolio_id,
                    harvests=arguments["harvests"],
                    tax_rate_short=arguments.get("tax_rate_short", 0.37),
                    tax_rate_long=arguments.get("tax_rate_long", 0.20),
                )
            else:
                result = {"error": f"Unknown tool: {name}"}
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc)
            result = {"error": str(exc)}

    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    await init_db()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
