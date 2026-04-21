from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..auth import get_current_user
from ..models.models import Portfolio, Client


async def assert_portfolio_access(
    portfolio_id: int,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Portfolio:
    """Fetch portfolio and verify the current user may access it.

    Admin → any portfolio.
    Advisor → only portfolios belonging to their own clients.
    Individual → only portfolios belonging to their own self-client.
    """
    portfolio = await db.get(Portfolio, portfolio_id)
    if not portfolio:
        raise HTTPException(404, "Portfolio not found")
    if current_user.role == "admin":
        return portfolio
    if portfolio.client_id is None:
        raise HTTPException(403, "Access denied")
    client = await db.get(Client, portfolio.client_id)
    if not client or client.advisor_id != current_user.id:
        raise HTTPException(403, "Access denied")
    # individual persona: only their self-client counts
    if current_user.role == "individual" and not client.is_self:
        raise HTTPException(403, "Access denied")
    return portfolio
