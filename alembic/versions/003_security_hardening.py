"""003 — security hardening: idempotency records, system flags, encrypted text columns

Revision ID: 003
Revises: 002
Create Date: 2026-04-19

Notes
  - Encrypted columns (Transaction.notes, RecommendationLog.prompt/reasoning/
    tool_calls_json/draft_plan_json, AuditEvent.details_json) stay `TEXT` at
    the DB layer — the TypeDecorator handles ciphering. No schema change
    needed for those; existing plaintext rows remain readable since the
    decrypt helper falls back to plaintext when no `enc_v1:` marker is found.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", "user_id", "endpoint", name="uq_idem_key_user_endpoint"),
    )
    op.create_index("ix_idempotency_records_id", "idempotency_records", ["id"])
    op.create_index("ix_idempotency_records_key", "idempotency_records", ["key"])
    op.create_index("ix_idempotency_records_user_id", "idempotency_records", ["user_id"])
    op.create_index("ix_idempotency_records_created_at", "idempotency_records", ["created_at"])

    op.create_table(
        "system_flags",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("system_flags")
    op.drop_table("idempotency_records")
