"""006 — CPA invites table (Invite your CPA growth loop)

Revision ID: 006
Revises: 005
Create Date: 2026-04-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cpa_invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("cpa_email", sa.String(), nullable=False),
        sa.Column("cpa_name", sa.String(), nullable=True),
        sa.Column("firm_name", sa.String(), nullable=True),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("first_viewed_at", sa.DateTime(), nullable=True),
        sa.Column("last_viewed_at", sa.DateTime(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cpa_invites_id", "cpa_invites", ["id"])
    op.create_index("ix_cpa_invites_user_id", "cpa_invites", ["user_id"])
    op.create_index("ix_cpa_invites_portfolio_id", "cpa_invites", ["portfolio_id"])
    op.create_index("ix_cpa_invites_token_hash", "cpa_invites", ["token_hash"])
    op.create_index("ix_cpa_invites_created_at", "cpa_invites", ["created_at"])


def downgrade() -> None:
    op.drop_table("cpa_invites")
