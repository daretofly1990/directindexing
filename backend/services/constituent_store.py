"""
Cache store for index constituents.

Handles DB persistence and snapshot export.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.models import IndexConstituent
from .constituents import compute_weights, fetch_sp500, fetch_nasdaq100, fetch_russell1000

logger = logging.getLogger(__name__)

VALID_INDEXES = {"sp500", "nasdaq100", "russell1000"}

# Cap each index at the top-N by market-cap weight. Chosen to keep portfolio
# construction fast and broker-friendly (20 round-lot trades vs 500+). Weights
# are renormalized so the truncated set sums to 1.0.
TOP_N = 20


def _top_n_renormalized(constituents: list[dict]) -> list[dict]:
    """Sort by weight desc, take top N, renormalize weights to sum to 1.0."""
    if not constituents:
        return constituents
    sorted_rows = sorted(constituents, key=lambda c: c.get("weight", 0.0) or 0.0, reverse=True)
    top = sorted_rows[:TOP_N]
    total = sum((c.get("weight") or 0.0) for c in top)
    if total > 0:
        for c in top:
            c["weight"] = (c.get("weight") or 0.0) / total
    return top

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "constituents_snapshot.json"
)


async def get_constituents(db: AsyncSession, index_name: str) -> list[dict]:
    """Return active constituents for *index_name* as a list of dicts."""
    result = await db.execute(
        select(IndexConstituent).where(
            IndexConstituent.index_name == index_name,
            IndexConstituent.is_active == True,
        ).order_by(IndexConstituent.weight.desc())
    )
    rows = result.scalars().all()
    return [_row_to_dict(r) for r in rows]


async def last_refreshed(db: AsyncSession, index_name: str) -> Optional[datetime]:
    """Return the as_of timestamp of the most recently inserted active row, or None."""
    result = await db.execute(
        select(IndexConstituent.as_of)
        .where(
            IndexConstituent.index_name == index_name,
            IndexConstituent.is_active == True,
        )
        .order_by(IndexConstituent.as_of.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row


async def refresh_index(db: AsyncSession, index_name: str) -> list[dict]:
    """
    Fetch fresh constituent data, compute weights, persist to DB.

    Steps:
      1. Fetch raw constituent list from source.
      2. Compute market-cap weights via yfinance.
      3. Insert new rows with new as_of timestamp.
      4. Mark prior rows inactive.
      5. Export snapshot JSON.

    Returns the newly inserted rows as dicts.
    """
    if index_name not in VALID_INDEXES:
        raise ValueError(f"Unknown index: {index_name}. Must be one of {VALID_INDEXES}")

    as_of = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1. Fetch
    fetcher = {"sp500": fetch_sp500, "nasdaq100": fetch_nasdaq100, "russell1000": fetch_russell1000}
    raw = await fetcher[index_name]()

    # 2. Compute weights
    constituents = await compute_weights(raw)

    # 2b. Cap at top-N by weight and renormalize. The long tail of 450+ small-cap
    # names isn't usable in a retail direct-indexing product — it's too many
    # positions, too many tiny round-lot trades, and the marginal exposure at
    # each additional name is a rounding error.
    constituents = _top_n_renormalized(constituents)

    # 3 & 4. DB transaction: deactivate old, insert new
    async with db.begin_nested():
        # Mark old rows inactive
        await db.execute(
            update(IndexConstituent)
            .where(
                IndexConstituent.index_name == index_name,
                IndexConstituent.is_active == True,
            )
            .values(is_active=False)
        )

        # Insert new rows
        for c in constituents:
            db.add(IndexConstituent(
                index_name=index_name,
                symbol=c["symbol"],
                name=c.get("name", ""),
                sector=c.get("sector", ""),
                industry=c.get("industry", "") or None,
                weight=c.get("weight", 0.0),
                market_cap=c.get("market_cap"),
                as_of=as_of,
                is_active=True,
            ))

    await db.commit()

    inserted = await get_constituents(db, index_name)
    logger.info(
        "Refreshed %s: %d constituents, as_of=%s", index_name, len(inserted), as_of.isoformat()
    )

    # 5. Export snapshot
    await _export_snapshot(db)

    return inserted


async def _export_snapshot(db: AsyncSession) -> None:
    """Write all active constituents for all indexes to the JSON snapshot file."""
    snapshot: dict[str, list[dict]] = {}
    for idx in VALID_INDEXES:
        rows = await get_constituents(db, idx)
        snapshot[idx] = rows

    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, default=str)
    logger.info("Snapshot written to %s", SNAPSHOT_PATH)


def load_snapshot(index_name: str) -> list[dict]:
    """Load constituents from the offline JSON snapshot (sync)."""
    if not os.path.exists(SNAPSHOT_PATH):
        return []
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get(index_name, [])
    except Exception as exc:
        logger.warning("Could not load snapshot: %s", exc)
        return []


def _row_to_dict(row: IndexConstituent) -> dict:
    return {
        "symbol": row.symbol,
        "name": row.name,
        "sector": row.sector,
        "industry": row.industry,
        "weight": row.weight,
        "market_cap": row.market_cap,
        "as_of": row.as_of.isoformat() if row.as_of else None,
    }
