"""
Household routes: group Client records (spouse, IRAs, joint) for
household-scope wash-sale tracking.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ...auth import get_current_user
from ...database import get_db
from ...models.models import Client, Household

router = APIRouter(prefix="/households", tags=["households"])


class CreateHouseholdRequest(BaseModel):
    name: str


class AddClientRequest(BaseModel):
    client_id: int


@router.get("")
async def list_households(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Household)
    if current_user.role != "admin":
        stmt = stmt.where(Household.owner_user_id == current_user.id)
    result = await db.execute(stmt.order_by(Household.created_at.desc()))
    rows = result.scalars().all()
    return [_to_dict(h) for h in rows]


@router.post("")
async def create_household(
    req: CreateHouseholdRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    h = Household(name=req.name, owner_user_id=current_user.id)
    db.add(h)
    await db.commit()
    await db.refresh(h)
    return _to_dict(h)


@router.post("/{household_id}/clients")
async def add_client_to_household(
    household_id: int,
    req: AddClientRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    h = await db.get(Household, household_id)
    if not h or (current_user.role != "admin" and h.owner_user_id != current_user.id):
        raise HTTPException(404, "Household not found")
    client = await db.get(Client, req.client_id)
    if not client or (current_user.role != "admin" and client.advisor_id != current_user.id):
        raise HTTPException(404, "Client not found")
    client.household_id = household_id
    await db.commit()
    return {"household_id": household_id, "client_id": req.client_id}


def _to_dict(h: Household) -> dict:
    return {
        "id": h.id,
        "name": h.name,
        "owner_user_id": h.owner_user_id,
        "created_at": h.created_at.isoformat(),
    }
