"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=True),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_id", "users", ["id"], unique=False)

    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("advisor_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("tax_rate_short", sa.Float(), nullable=True),
        sa.Column("tax_rate_long", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["advisor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_clients_id", "clients", ["id"], unique=False)

    op.create_table(
        "portfolios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("initial_value", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_portfolios_id", "portfolios", ["id"], unique=False)

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("sector", sa.String(), nullable=True),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("avg_cost_basis", sa.Float(), nullable=False),
        sa.Column("target_weight", sa.Float(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_positions_id", "positions", ["id"], unique=False)

    op.create_table(
        "tax_lots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("cost_basis", sa.Float(), nullable=False),
        sa.Column("purchase_date", sa.DateTime(), nullable=False),
        sa.Column("sale_date", sa.DateTime(), nullable=True),
        sa.Column("proceeds", sa.Float(), nullable=True),
        sa.Column("realized_gain_loss", sa.Float(), nullable=True),
        sa.Column("wash_sale_disallowed", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tax_lots_id", "tax_lots", ["id"], unique=False)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("transaction_type", sa.String(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("total_value", sa.Float(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transactions_id", "transactions", ["id"], unique=False)

    op.create_table(
        "esg_exclusions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("exclusion_type", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_esg_exclusions_id", "esg_exclusions", ["id"], unique=False)

    op.create_table(
        "corporate_action_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("old_rate", sa.Float(), nullable=True),
        sa.Column("new_rate", sa.Float(), nullable=True),
        sa.Column("ratio", sa.Float(), nullable=False),
        sa.Column("ex_date", sa.DateTime(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("positions_affected", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_corporate_action_logs_id", "corporate_action_logs", ["id"], unique=False)
    op.create_index("ix_corporate_action_logs_symbol", "corporate_action_logs", ["symbol"], unique=False)


    op.create_table(
        "index_constituents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("index_name", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("sector", sa.String(), nullable=True),
        sa.Column("industry", sa.String(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("market_cap", sa.Float(), nullable=True),
        sa.Column("as_of", sa.DateTime(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_index_constituents_id", "index_constituents", ["id"], unique=False)
    op.create_index(
        "ix_index_constituents_index_active",
        "index_constituents",
        ["index_name", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("index_constituents")
    op.drop_table("corporate_action_logs")
    op.drop_table("esg_exclusions")
    op.drop_table("transactions")
    op.drop_table("tax_lots")
    op.drop_table("positions")
    op.drop_table("portfolios")
    op.drop_table("clients")
    op.drop_table("users")
