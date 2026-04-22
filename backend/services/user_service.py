"""User management: creation, authentication, password hashing."""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from passlib.context import CryptContext

from ..models.models import User

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


async def create_user(
    db: AsyncSession,
    email: str,
    password: str,
    role: str = "advisor",
    full_name: str | None = None,
) -> User:
    user = User(
        email=email.lower().strip(),
        hashed_password=hash_password(password),
        role=role,
        full_name=full_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def create_individual_user(
    db: AsyncSession,
    email: str,
    password: str,
    full_name: str | None = None,
    tax_rate_short: float = 0.37,
    tax_rate_long: float = 0.20,
):
    # Returns (User, Client). Not type-annotated to avoid a forward-reference
    # import cycle — Client is imported lazily below.
    """
    Sign up a retail individual: creates the User (role=individual) AND a
    self-Client record in a single atomic flow. The self-client owns all
    portfolios the individual creates.
    """
    from ..models.models import Client
    user = User(
        email=email.lower().strip(),
        hashed_password=hash_password(password),
        role="individual",
        full_name=full_name,
    )
    db.add(user)
    await db.flush()

    client = Client(
        advisor_id=user.id,   # self-managed: advisor_id points to themselves
        name=full_name or email,
        email=email,
        tax_rate_short=tax_rate_short,
        tax_rate_long=tax_rate_long,
        is_self=True,
    )
    db.add(client)
    await db.commit()
    await db.refresh(user)
    await db.refresh(client)
    return user, client


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower().strip()))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


async def get_all_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.created_at))
    return result.scalars().all()


async def ensure_admin(db: AsyncSession, email: str, password: str) -> None:
    """Create the bootstrap admin user if no users exist yet."""
    result = await db.execute(select(User))
    if result.scalars().first() is None:
        await create_user(db, email, password, role="admin", full_name="Admin")
