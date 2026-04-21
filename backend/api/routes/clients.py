from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ...database import get_db
from ...auth import get_current_user, TokenData, verify_token
from ...models.models import Client

router = APIRouter(prefix="/clients", tags=["clients"])


class CreateClientRequest(BaseModel):
    name: str
    email: str | None = None
    tax_rate_short: float = 0.37
    tax_rate_long: float = 0.20


class UpdateClientRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    tax_rate_short: float | None = None
    tax_rate_long: float | None = None


@router.get("")
async def list_clients(current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user.role == "admin":
        result = await db.execute(select(Client).order_by(Client.created_at.desc()))
    else:
        result = await db.execute(
            select(Client).where(Client.advisor_id == current_user.id).order_by(Client.created_at.desc())
        )
    clients = result.scalars().all()
    return [_to_dict(c) for c in clients]


@router.post("")
async def create_client(
    req: CreateClientRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    client = Client(
        advisor_id=current_user.id,
        name=req.name,
        email=req.email,
        tax_rate_short=req.tax_rate_short,
        tax_rate_long=req.tax_rate_long,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    return _to_dict(client)


@router.patch("/{client_id}")
async def update_client(
    client_id: int,
    req: UpdateClientRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(404, "Client not found")
    if current_user.role != "admin" and client.advisor_id != current_user.id:
        raise HTTPException(403, "Not your client")
    if req.name is not None:
        client.name = req.name
    if req.email is not None:
        client.email = req.email
    if req.tax_rate_short is not None:
        client.tax_rate_short = req.tax_rate_short
    if req.tax_rate_long is not None:
        client.tax_rate_long = req.tax_rate_long
    await db.commit()
    return _to_dict(client)


def _to_dict(c: Client) -> dict:
    return {
        "id": c.id,
        "advisor_id": c.advisor_id,
        "name": c.name,
        "email": c.email,
        "tax_rate_short": c.tax_rate_short,
        "tax_rate_long": c.tax_rate_long,
        "created_at": c.created_at.isoformat(),
    }
