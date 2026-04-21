"""
Spec-ID sale execution: user-selected lots closed at live (or override) price.

Unlike `tax_loss_service.execute_harvest` — which closes an entire position with
a chosen method — this service closes specific tax lots identified by ID. It
records a Transaction, credits the portfolio cash, reduces position shares, and
enforces wash-sale on any realized loss.
"""
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import Portfolio, Position, TaxLot, Transaction
from .ai_guardrails import check_manual_sell_cap
from .audit import log_audit
from .finnhub_client import finnhub_client
from .kill_switch import assert_trading_enabled
from .lot_engine import lot_engine


async def execute_spec_id_sale(
    db: AsyncSession,
    portfolio_id: int,
    position_id: int,
    lot_ids: list[int],
    override_price: float | None = None,
    user_id: int | None = None,
    ip_address: str | None = None,
) -> dict:
    """Close the given lots at live (or override) price, record the trade, update the position."""
    if not lot_ids:
        raise ValueError("lot_ids must not be empty")

    # Kill switch — refuse all trade activity when halted
    await assert_trading_enabled(db)

    pos = await db.get(Position, position_id)
    if not pos or pos.portfolio_id != portfolio_id:
        raise ValueError("Position not found")

    # Verify all requested lots belong to this position AND are still open
    lot_result = await db.execute(
        select(TaxLot).where(TaxLot.id.in_(lot_ids))
    )
    lots = lot_result.scalars().all()
    if len(lots) != len(lot_ids):
        missing = set(lot_ids) - {l.id for l in lots}
        raise ValueError(f"Lots not found: {sorted(missing)}")
    for lot in lots:
        if lot.position_id != position_id:
            raise ValueError(f"Lot {lot.id} does not belong to position {position_id}")
        if lot.sale_date is not None or lot.shares <= 0:
            raise ValueError(f"Lot {lot.id} is already closed")

    if override_price is not None:
        price = override_price
    else:
        quote = await finnhub_client.get_quote(pos.symbol)
        price = quote.get("current_price") or pos.avg_cost_basis
    if price <= 0:
        raise ValueError("Could not determine sale price")

    # Pre-flight sell cap: total_shares * price must not exceed MAX_SELL_PCT of NAV
    projected_shares = sum(l.shares for l in lots)
    projected_notional = projected_shares * price
    within_cap, reason, _ = await check_manual_sell_cap(db, portfolio_id, projected_notional)
    if not within_cap:
        raise ValueError(f"SELL_CAP_EXCEEDED: {reason}")

    now = datetime.utcnow()

    # Close lots via engine
    close_result = await lot_engine.close_lots_by_ids(
        db=db,
        lot_ids=lot_ids,
        sale_price=price,
        sale_date=now,
    )

    total_shares = sum(d["shares"] for d in close_result["closed_lots"])
    proceeds = sum(d["proceeds"] for d in close_result["closed_lots"])
    economic_gain_loss = close_result["total_gain"]

    # Wash-sale check: was this symbol purchased within 30 days before sale?
    pre_sale_buys = await lot_engine.check_wash_sale(db, portfolio_id, pos.symbol, now)
    wash_sale_triggered = len(pre_sale_buys) > 0

    disallowed_amount = 0.0
    if wash_sale_triggered and economic_gain_loss < 0:
        disallowed_amount = abs(economic_gain_loss)
        await lot_engine.disallow_loss_on_lots(db, position_id, disallowed_amount, now)

    # Update position shares; deactivate if all shares closed
    pos.shares = max(pos.shares - total_shares, 0.0)
    if pos.shares <= 1e-9:
        pos.shares = 0.0
        pos.is_active = False

    notes = (
        f"Spec-ID sale of {len(lot_ids)} lot(s) at ${price:.2f}. "
        f"ST: ${close_result['short_term_gain']:,.2f}, "
        f"LT: ${close_result['long_term_gain']:,.2f}."
    )
    if wash_sale_triggered:
        notes += f" WASH SALE: ${disallowed_amount:,.2f} loss disallowed."

    txn = Transaction(
        portfolio_id=portfolio_id,
        symbol=pos.symbol,
        transaction_type="SELL",
        shares=total_shares,
        price=price,
        total_value=proceeds,
        notes=notes,
    )
    db.add(txn)

    portfolio = await db.get(Portfolio, portfolio_id)
    portfolio.cash += proceeds

    # Audit trail: who sold what, when, at what price
    await log_audit(
        db,
        event_type="MANUAL_SELL",
        user_id=user_id,
        portfolio_id=portfolio_id,
        object_type="position",
        object_id=position_id,
        details={
            "symbol": pos.symbol, "lot_ids": lot_ids, "shares": total_shares,
            "price": price, "proceeds": proceeds,
            "economic_gain_loss": economic_gain_loss,
            "wash_sale_disallowed": disallowed_amount,
        },
        ip_address=ip_address,
    )

    await db.commit()

    recognizable = economic_gain_loss + disallowed_amount
    return {
        "symbol": pos.symbol,
        "lot_ids": lot_ids,
        "shares_sold": total_shares,
        "sale_price": price,
        "proceeds": proceeds,
        "short_term_gain_loss": close_result["short_term_gain"],
        "long_term_gain_loss": close_result["long_term_gain"],
        "economic_gain_loss": economic_gain_loss,
        "wash_sale_triggered": wash_sale_triggered,
        "wash_sale_disallowed": disallowed_amount,
        "recognizable_gain_loss": recognizable,
        "closed_lots": close_result["closed_lots"],
        "remaining_shares": pos.shares,
        "position_active": pos.is_active,
    }
