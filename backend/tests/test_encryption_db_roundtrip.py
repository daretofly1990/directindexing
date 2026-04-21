"""
DB-level encryption round-trip:

  1. Monkey-patch the already-loaded encryption module to use a fresh Fernet
     (no reimport, no module reload — that was breaking other tests by
     creating a second Base metadata)
  2. Create an in-memory DB, insert a Transaction with `notes` (encrypted col)
  3. Read it back via ORM — value should decrypt to the original string
  4. Read the same row via raw SQL — the column stored on disk must be
     ciphertext with the `enc_v1:` marker, NOT the plaintext

The final raw-SQL check is the one that actually proves cipher-at-rest.
"""
import base64
from datetime import datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.models import Transaction, Portfolio
from backend.services import encryption as enc_mod


FERNET_KEY = base64.urlsafe_b64encode(b"\x42" * 32).decode()


@pytest_asyncio.fixture
async def encrypted_db(monkeypatch):
    """
    Swap the encryption module's _FERNET to a real MultiFernet for the test,
    restore it after. No module reload — other tests' imports stay intact.
    """
    real_fernet = MultiFernet([Fernet(FERNET_KEY.encode())])
    monkeypatch.setattr(enc_mod, "_FERNET", real_fernet)
    monkeypatch.setattr(enc_mod, "_KEYS", [FERNET_KEY.encode()])

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session, engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_notes_roundtrip(encrypted_db):
    session, engine = encrypted_db
    port = Portfolio(name="T", initial_value=1000, cash=0)
    session.add(port)
    await session.commit()
    await session.refresh(port)

    secret = "Sold 100 AAPL for Jane Doe — tax loss $4,320"
    txn = Transaction(
        portfolio_id=port.id, symbol="AAPL", transaction_type="SELL",
        shares=100, price=150, total_value=15000,
        timestamp=datetime.utcnow(), notes=secret,
    )
    session.add(txn)
    await session.commit()
    await session.refresh(txn)

    # ORM read — should decrypt transparently
    assert txn.notes == secret

    # Raw SQL read — must be ciphertext, not plaintext
    async with engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT notes FROM transactions WHERE id = :i"), {"i": txn.id}
        )).first()
    assert row is not None
    raw = row[0]
    assert raw is not None
    assert raw != secret, "Plaintext leaked into DB column — encryption is not active"
    assert raw.startswith("enc_v1:"), f"Expected enc_v1: marker, got: {raw[:30]}"


@pytest.mark.asyncio
async def test_null_notes_passthrough(encrypted_db):
    session, engine = encrypted_db
    port = Portfolio(name="T", initial_value=1000, cash=0)
    session.add(port)
    await session.commit()
    await session.refresh(port)

    txn = Transaction(
        portfolio_id=port.id, symbol="AAPL", transaction_type="BUY",
        shares=10, price=100, total_value=1000,
        timestamp=datetime.utcnow(), notes=None,
    )
    session.add(txn)
    await session.commit()
    await session.refresh(txn)
    assert txn.notes is None
