"""
Tax-lot engine: per-lot cost basis, FIFO/LIFO/HIFO/MIN_TERM lot selection,
short- vs. long-term gain classification, and 30-day wash-sale enforcement.
"""
from enum import Enum
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.models import TaxLot, Position, Transaction

LONG_TERM_DAYS = 365
WASH_SALE_WINDOW = 30  # days before AND after a loss sale


class LotSelectionMethod(str, Enum):
    FIFO = "fifo"        # oldest lots first — IRS default
    LIFO = "lifo"        # newest lots first
    HIFO = "hifo"        # highest cost basis first — maximises loss / minimises gain
    MIN_TERM = "min_term"  # prefer long-term lots first to avoid short-term gains
    SPEC_ID = "spec_id"  # user selects specific lots by ID


def _is_long_term(purchase_date: datetime, sale_date: datetime) -> bool:
    return (sale_date - purchase_date).days >= LONG_TERM_DAYS


def _sort_lots(lots: list[TaxLot], method: LotSelectionMethod, sale_date: datetime) -> list[TaxLot]:
    if method == LotSelectionMethod.FIFO:
        return sorted(lots, key=lambda l: l.purchase_date)
    if method == LotSelectionMethod.LIFO:
        return sorted(lots, key=lambda l: l.purchase_date, reverse=True)
    if method == LotSelectionMethod.HIFO:
        return sorted(lots, key=lambda l: l.cost_basis, reverse=True)
    if method == LotSelectionMethod.MIN_TERM:
        # Long-term lots first (0), then FIFO within each tier
        return sorted(lots, key=lambda l: (
            0 if _is_long_term(l.purchase_date, sale_date) else 1,
            l.purchase_date,
        ))
    return lots


class LotEngine:

    async def open_lot(
        self,
        db: AsyncSession,
        position_id: int,
        shares: float,
        cost_basis_per_share: float,
        purchase_date: datetime,
        wash_sale_adjustment: float = 0.0,
    ) -> TaxLot:
        """Create a new open tax lot. cost_basis_per_share already includes any wash-sale adjustment."""
        lot = TaxLot(
            position_id=position_id,
            shares=shares,
            cost_basis=cost_basis_per_share + wash_sale_adjustment,
            purchase_date=purchase_date,
            wash_sale_disallowed=0.0,
        )
        db.add(lot)
        return lot

    async def close_lots(
        self,
        db: AsyncSession,
        position_id: int,
        shares_to_sell: float,
        sale_price: float,
        sale_date: datetime,
        method: LotSelectionMethod = LotSelectionMethod.HIFO,
    ) -> dict:
        """
        Select and close lots covering shares_to_sell.
        Splits partial lots, returns gain/loss breakdown by holding period.
        """
        result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == position_id,
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            )
        )
        open_lots = result.scalars().all()
        sorted_lots = _sort_lots(open_lots, method, sale_date)

        short_term_gain = 0.0
        long_term_gain = 0.0
        closed_detail = []
        remaining = shares_to_sell

        for lot in sorted_lots:
            if remaining <= 0:
                break
            take = min(lot.shares, remaining)
            remaining -= take

            proceeds = take * sale_price
            cost = take * lot.cost_basis
            gain_loss = proceeds - cost
            is_lt = _is_long_term(lot.purchase_date, sale_date)
            holding_days = (sale_date - lot.purchase_date).days

            if is_lt:
                long_term_gain += gain_loss
            else:
                short_term_gain += gain_loss

            if take >= lot.shares - 1e-9:
                # Close the entire lot
                lot.sale_date = sale_date
                lot.proceeds = proceeds
                lot.realized_gain_loss = gain_loss
                lot.shares = 0.0
            else:
                # Partial close: shrink original lot, create a closed child lot
                lot.shares -= take
                closed = TaxLot(
                    position_id=position_id,
                    shares=take,
                    cost_basis=lot.cost_basis,
                    purchase_date=lot.purchase_date,
                    sale_date=sale_date,
                    proceeds=proceeds,
                    realized_gain_loss=gain_loss,
                    wash_sale_disallowed=0.0,
                )
                db.add(closed)

            closed_detail.append({
                "shares": take,
                "cost_basis_per_share": lot.cost_basis,
                "sale_price": sale_price,
                "proceeds": proceeds,
                "gain_loss": gain_loss,
                "holding_days": holding_days,
                "is_long_term": is_lt,
                "purchase_date": lot.purchase_date.isoformat(),
            })

        return {
            "closed_lots": closed_detail,
            "short_term_gain": short_term_gain,
            "long_term_gain": long_term_gain,
            "total_gain": short_term_gain + long_term_gain,
            "unmatched_shares": max(remaining, 0.0),
        }

    async def check_wash_sale(
        self,
        db: AsyncSession,
        portfolio_id: int,
        symbol: str,
        sale_date: datetime,
    ) -> list[Transaction]:
        """
        Return any BUY transactions for symbol within 30 days BEFORE the proposed sale.
        If any exist, the loss from this sale will be at least partially disallowed.
        (The 30-day post-sale window is enforced by blocking repurchase in TLH logic.)
        """
        window_start = sale_date - timedelta(days=WASH_SALE_WINDOW)
        result = await db.execute(
            select(Transaction).where(
                Transaction.portfolio_id == portfolio_id,
                Transaction.symbol == symbol,
                Transaction.transaction_type == "BUY",
                Transaction.timestamp >= window_start,
                Transaction.timestamp <= sale_date,
            )
        )
        return result.scalars().all()

    async def disallow_loss_on_lots(
        self,
        db: AsyncSession,
        position_id: int,
        disallowed_amount: float,
        sale_date: datetime,
    ) -> None:
        """
        Mark the most-recently-closed lots for this position as wash-sale impacted.
        The disallowed amount is stamped on those lots for tax reporting.
        """
        result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == position_id,
                TaxLot.sale_date == sale_date,
            )
        )
        lots = result.scalars().all()
        if not lots:
            return
        # Distribute disallowed amount proportionally across closed lots
        total_loss = sum(abs(l.realized_gain_loss or 0) for l in lots if (l.realized_gain_loss or 0) < 0)
        for lot in lots:
            if total_loss > 0 and (lot.realized_gain_loss or 0) < 0:
                share = abs(lot.realized_gain_loss) / total_loss
                lot.wash_sale_disallowed = disallowed_amount * share

    async def apply_wash_sale_to_replacement(
        self,
        db: AsyncSession,
        portfolio_id: int,
        replacement_symbol: str,
        disallowed_amount: float,
    ) -> None:
        """
        Add the disallowed wash-sale loss to the cost basis of the newest open lot
        for the replacement security, so the loss is deferred rather than permanently lost.
        """
        pos_result = await db.execute(
            select(Position).where(
                Position.portfolio_id == portfolio_id,
                Position.symbol == replacement_symbol,
                Position.is_active == True,  # noqa: E712
            )
        )
        pos = pos_result.scalar_one_or_none()
        if not pos:
            return
        lot_result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == pos.id,
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            ).order_by(TaxLot.purchase_date.desc())
        )
        lot = lot_result.scalars().first()
        if lot and lot.shares > 0:
            adjustment_per_share = disallowed_amount / lot.shares
            lot.cost_basis += adjustment_per_share

    async def close_lots_by_ids(
        self,
        db: AsyncSession,
        lot_ids: list[int],
        sale_price: float,
        sale_date: datetime,
    ) -> dict:
        """
        Spec-ID: close specific lots identified by the user.
        Each lot is fully closed at sale_price; partial-lot Spec-ID is not supported
        (IRS requires adequate identification at the time of sale).
        """
        result = await db.execute(
            select(TaxLot).where(
                TaxLot.id.in_(lot_ids),
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            )
        )
        lots = result.scalars().all()

        found_ids = {lot.id for lot in lots}
        missing = [i for i in lot_ids if i not in found_ids]
        if missing:
            raise ValueError(f"Lots not found or already closed: {missing}")

        short_term_gain = 0.0
        long_term_gain = 0.0
        closed_detail = []

        for lot in lots:
            shares_closed = lot.shares
            proceeds = shares_closed * sale_price
            cost = shares_closed * lot.cost_basis
            gain_loss = proceeds - cost
            is_lt = _is_long_term(lot.purchase_date, sale_date)
            holding_days = (sale_date - lot.purchase_date).days

            if is_lt:
                long_term_gain += gain_loss
            else:
                short_term_gain += gain_loss

            lot.sale_date = sale_date
            lot.proceeds = proceeds
            lot.realized_gain_loss = gain_loss
            lot.shares = 0.0

            closed_detail.append({
                "lot_id": lot.id,
                "shares": shares_closed,
                "cost_basis_per_share": lot.cost_basis,
                "sale_price": sale_price,
                "proceeds": proceeds,
                "gain_loss": gain_loss,
                "holding_days": holding_days,
                "is_long_term": is_lt,
                "purchase_date": lot.purchase_date.isoformat(),
            })

        return {
            "closed_lots": closed_detail,
            "short_term_gain": short_term_gain,
            "long_term_gain": long_term_gain,
            "total_gain": short_term_gain + long_term_gain,
            "unmatched_shares": 0.0,
            "method": "spec_id",
        }

    async def get_open_lots(self, db: AsyncSession, position_id: int) -> list[TaxLot]:
        result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id == position_id,
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            ).order_by(TaxLot.purchase_date)
        )
        return result.scalars().all()

    async def get_realized_gain_summary(self, db: AsyncSession, portfolio_id: int) -> dict:
        """Aggregate all closed lots into short-term / long-term buckets for tax reporting."""
        pos_result = await db.execute(
            select(Position).where(Position.portfolio_id == portfolio_id)
        )
        positions = pos_result.scalars().all()
        position_ids = [p.id for p in positions]

        if not position_ids:
            return self._empty_summary()

        lot_result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id.in_(position_ids),
                TaxLot.sale_date != None,  # noqa: E711
            )
        )
        closed_lots = lot_result.scalars().all()

        st_gains = st_losses = lt_gains = lt_losses = 0.0
        wash_sale_total = 0.0
        lot_rows = []

        pos_map = {p.id: p for p in positions}

        for lot in closed_lots:
            gl = lot.realized_gain_loss or 0.0
            recognizable = gl + (lot.wash_sale_disallowed or 0.0)
            is_lt = _is_long_term(lot.purchase_date, lot.sale_date)
            wash_sale_total += lot.wash_sale_disallowed or 0.0

            if is_lt:
                if recognizable >= 0:
                    lt_gains += recognizable
                else:
                    lt_losses += recognizable
            else:
                if recognizable >= 0:
                    st_gains += recognizable
                else:
                    st_losses += recognizable

            pos = pos_map.get(lot.position_id)
            lot_rows.append({
                "symbol": pos.symbol if pos else "?",
                "shares": lot.shares if lot.sale_date and lot.shares == 0 else (lot.shares or 0),
                "cost_basis_per_share": lot.cost_basis,
                "purchase_date": lot.purchase_date.isoformat(),
                "sale_date": lot.sale_date.isoformat(),
                "proceeds": lot.proceeds or 0.0,
                "gain_loss": gl,
                "wash_sale_disallowed": lot.wash_sale_disallowed or 0.0,
                "recognizable_gain_loss": recognizable,
                "is_long_term": is_lt,
                "holding_days": (lot.sale_date - lot.purchase_date).days,
            })

        return {
            "short_term_gains": st_gains,
            "short_term_losses": st_losses,
            "short_term_net": st_gains + st_losses,
            "long_term_gains": lt_gains,
            "long_term_losses": lt_losses,
            "long_term_net": lt_gains + lt_losses,
            "total_net_gain_loss": st_gains + st_losses + lt_gains + lt_losses,
            "wash_sale_disallowed_total": wash_sale_total,
            "closed_lots": lot_rows,
        }

    def _empty_summary(self) -> dict:
        return {
            "short_term_gains": 0.0,
            "short_term_losses": 0.0,
            "short_term_net": 0.0,
            "long_term_gains": 0.0,
            "long_term_losses": 0.0,
            "long_term_net": 0.0,
            "total_net_gain_loss": 0.0,
            "wash_sale_disallowed_total": 0.0,
            "closed_lots": [],
        }

    async def get_open_lot_detail(self, db: AsyncSession, portfolio_id: int) -> list[dict]:
        """Return all open lots across a portfolio with unrealized P&L metadata."""
        pos_result = await db.execute(
            select(Position).where(
                Position.portfolio_id == portfolio_id,
                Position.is_active == True,  # noqa: E712
            )
        )
        positions = pos_result.scalars().all()
        pos_map = {p.id: p for p in positions}

        lot_result = await db.execute(
            select(TaxLot).where(
                TaxLot.position_id.in_([p.id for p in positions]),
                TaxLot.sale_date == None,  # noqa: E711
                TaxLot.shares > 0,
            ).order_by(TaxLot.purchase_date)
        )
        open_lots = lot_result.scalars().all()

        now = datetime.utcnow()
        rows = []
        for lot in open_lots:
            pos = pos_map.get(lot.position_id)
            holding_days = (now - lot.purchase_date).days
            rows.append({
                "lot_id": lot.id,
                "symbol": pos.symbol if pos else "?",
                "sector": pos.sector if pos else None,
                "shares": lot.shares,
                "cost_basis_per_share": lot.cost_basis,
                "purchase_date": lot.purchase_date.isoformat(),
                "holding_days": holding_days,
                "is_long_term": holding_days >= LONG_TERM_DAYS,
                "days_until_long_term": max(0, LONG_TERM_DAYS - holding_days),
            })
        return rows


lot_engine = LotEngine()
