"""M1-M6: individual persona, households, acknowledgements, trade plans, recommendation logs, audit, delisting

Revision ID: 002
Revises: 001
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- users: email_verified column ---
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()))

    # --- households ---
    op.create_table(
        "households",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_households_id", "households", ["id"])

    # --- clients: is_self, household_id ---
    with op.batch_alter_table("clients") as b:
        b.add_column(sa.Column("is_self", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("household_id", sa.Integer(), nullable=True))
        b.create_foreign_key("fk_clients_household_id", "households", ["household_id"], ["id"])

    # --- acknowledgements ---
    op.create_table(
        "acknowledgements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("document_type", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_acknowledgements_id", "acknowledgements", ["id"])
    op.create_index("ix_acknowledgements_user_id", "acknowledgements", ["user_id"])

    # --- positions: delisting flag ---
    with op.batch_alter_table("positions") as b:
        b.add_column(sa.Column("is_delisted", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("delisted_at", sa.DateTime(), nullable=True))

    # --- recommendation_logs ---
    op.create_table(
        "recommendation_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("tool_calls_json", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("draft_plan_json", sa.Text(), nullable=True),
        sa.Column("adv_version_acknowledged", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("demo_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recommendation_logs_id", "recommendation_logs", ["id"])
    op.create_index("ix_recommendation_logs_user_id", "recommendation_logs", ["user_id"])
    op.create_index("ix_recommendation_logs_portfolio_id", "recommendation_logs", ["portfolio_id"])
    op.create_index("ix_recommendation_logs_created_at", "recommendation_logs", ["created_at"])

    # --- audit_events ---
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=True),
        sa.Column("object_type", sa.String(), nullable=True),
        sa.Column("object_id", sa.Integer(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_id", "audit_events", ["id"])
    op.create_index("ix_audit_events_user_id", "audit_events", ["user_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_portfolio_id", "audit_events", ["portfolio_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])

    # --- trade_plans ---
    op.create_table(
        "trade_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("draft_plan", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("recommendation_log_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["recommendation_log_id"], ["recommendation_logs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trade_plans_id", "trade_plans", ["id"])
    op.create_index("ix_trade_plans_portfolio_id", "trade_plans", ["portfolio_id"])

    # --- trade_plan_items ---
    op.create_table(
        "trade_plan_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("est_price", sa.Float(), nullable=True),
        sa.Column("est_proceeds", sa.Float(), nullable=True),
        sa.Column("lot_ids_json", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["trade_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trade_plan_items_id", "trade_plan_items", ["id"])
    op.create_index("ix_trade_plan_items_plan_id", "trade_plan_items", ["plan_id"])


def downgrade() -> None:
    op.drop_table("trade_plan_items")
    op.drop_table("trade_plans")
    op.drop_table("audit_events")
    op.drop_table("recommendation_logs")
    with op.batch_alter_table("positions") as b:
        b.drop_column("delisted_at")
        b.drop_column("is_delisted")
    op.drop_table("acknowledgements")
    with op.batch_alter_table("clients") as b:
        b.drop_constraint("fk_clients_household_id", type_="foreignkey")
        b.drop_column("household_id")
        b.drop_column("is_self")
    op.drop_table("households")
    with op.batch_alter_table("users") as b:
        b.drop_column("email_verified")
