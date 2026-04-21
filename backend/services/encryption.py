"""
Application-level encryption for PII / sensitive columns.

Usage:
    from backend.services.encryption import EncryptedText
    notes = Column(EncryptedText, nullable=True)

Design
  - `FIELD_ENCRYPTION_KEYS` env var: comma-separated urlsafe-base64 Fernet keys.
    First key is the primary (used for encrypt), all keys are tried on decrypt —
    this supports key rotation without downtime.
  - If the env var is unset, encryption is a no-op (plaintext). The app logs a
    warning at startup but does not crash — useful for dev / early deployment.
  - Ciphertext columns stay `String`/`Text` at the DB layer; the TypeDecorator
    transparently ciphers on write and deciphers on read.
  - We prefix all encrypted values with a marker (`enc_v1:`) so mixed-mode
    databases (some rows encrypted, some plaintext, during rollout) don't break
    on read.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

MARKER = "enc_v1:"


def _load_keys() -> list[bytes]:
    raw = os.getenv("FIELD_ENCRYPTION_KEYS", "").strip()
    if not raw:
        return []
    return [k.strip().encode() for k in raw.split(",") if k.strip()]


_KEYS = _load_keys()

try:
    from cryptography.fernet import Fernet, InvalidToken, MultiFernet
    if _KEYS:
        _FERNET: MultiFernet | None = MultiFernet([Fernet(k) for k in _KEYS])
    else:
        _FERNET = None
        logger.warning(
            "FIELD_ENCRYPTION_KEYS is unset — PII columns will be stored in plaintext. "
            "Generate a key with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
except ImportError:
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore
    MultiFernet = None  # type: ignore
    _FERNET = None
    logger.warning("`cryptography` not installed — PII columns will be stored in plaintext.")


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    if _FERNET is None:
        return plaintext
    return MARKER + _FERNET.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str | None:
    if ciphertext is None:
        return None
    if not ciphertext.startswith(MARKER):
        return ciphertext  # plaintext row written before encryption was enabled
    if _FERNET is None:
        # Encrypted value in DB but no key loaded — refuse silently (admin must load key)
        logger.error("Encountered enc_v1-prefixed row but no decryption key is loaded.")
        return None
    try:
        return _FERNET.decrypt(ciphertext[len(MARKER):].encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt enc_v1 row — key rotation may have dropped the encrypting key.")
        return None


class EncryptedText(TypeDecorator):
    """SQLAlchemy type that encrypts text at write, decrypts at read."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # writing to DB
        return encrypt(value)

    def process_result_value(self, value, dialect):  # reading from DB
        return decrypt(value)
