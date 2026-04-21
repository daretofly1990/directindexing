"""005 — admin TOTP MFA columns on users

Revision ID: 005
Revises: 004
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("totp_secret", sa.Text(), nullable=True))
        b.add_column(sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("totp_enabled")
        b.drop_column("totp_secret")
