from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from ..database import Base
from ..services.encryption import EncryptedText


class User(Base):
    """Platform users — admins, advisors, and individual retail users."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False, default="advisor")   # admin | advisor | individual
    is_active = Column(Boolean, default=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    # TOTP / admin MFA. `totp_secret` is base32 (pyotp default). When set,
    # admin login must present a valid 6-digit code from an authenticator app.
    totp_secret = Column(EncryptedText, nullable=True)
    totp_enabled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    clients = relationship("Client", back_populates="advisor")
    acknowledgements = relationship("Acknowledgement", back_populates="user", cascade="all, delete-orphan")


class Household(Base):
    """Wash-sale scope: groups Clients that share tax treatment (spouse + IRAs)."""
    __tablename__ = "households"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    clients = relationship("Client", back_populates="household")


class Acknowledgement(Base):
    """Captures ToS / ADV-Part-2A / Privacy Notice acceptance by a user."""
    __tablename__ = "acknowledgements"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    document_type = Column(String, nullable=False)  # tos | adv_part_2a | privacy
    version = Column(String, nullable=False)        # hash or semver of the doc
    accepted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ip_address = Column(String, nullable=True)
    user = relationship("User", back_populates="acknowledgements")


class Client(Base):
    """
    End client: either an advisor's managed client or an individual user's own
    self-client. `is_self=True` means this Client is auto-created for an
    `individual` user and is 1:1 with a User row.
    """
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True, index=True)
    advisor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True)
    tax_rate_short = Column(Float, default=0.37)   # short-term capital gains rate
    tax_rate_long = Column(Float, default=0.20)    # long-term capital gains rate
    is_self = Column(Boolean, nullable=False, default=False)  # individual-persona self-client
    household_id = Column(Integer, ForeignKey("households.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    advisor = relationship("User", back_populates="clients")
    portfolios = relationship("Portfolio", back_populates="client")
    household = relationship("Household", back_populates="clients")


class Portfolio(Base):
    __tablename__ = "portfolios"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    initial_value = Column(Float, nullable=False)
    cash = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    client = relationship("Client", back_populates="portfolios")
    positions = relationship("Position", back_populates="portfolio", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="portfolio", cascade="all, delete-orphan")
    esg_exclusions = relationship("ESGExclusion", back_populates="portfolio", cascade="all, delete-orphan")

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    symbol = Column(String, nullable=False)
    name = Column(String)
    sector = Column(String)
    shares = Column(Float, nullable=False)
    avg_cost_basis = Column(Float, nullable=False)
    target_weight = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)
    is_delisted = Column(Boolean, nullable=False, default=False)
    delisted_at = Column(DateTime, nullable=True)
    portfolio = relationship("Portfolio", back_populates="positions")
    tax_lots = relationship("TaxLot", back_populates="position", cascade="all, delete-orphan")

class TaxLot(Base):
    __tablename__ = "tax_lots"
    id = Column(Integer, primary_key=True, index=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    shares = Column(Float, nullable=False)
    cost_basis = Column(Float, nullable=False)          # per-share, may include wash-sale adjustment
    purchase_date = Column(DateTime, nullable=False)
    sale_date = Column(DateTime, nullable=True)         # None means lot is still open
    proceeds = Column(Float, nullable=True)             # total proceeds at sale
    realized_gain_loss = Column(Float, nullable=True)   # economic gain/loss (pre wash-sale)
    wash_sale_disallowed = Column(Float, nullable=False, default=0.0)  # loss disallowed by wash-sale rule
    position = relationship("Position", back_populates="tax_lots")

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    symbol = Column(String, nullable=False)
    transaction_type = Column(String, nullable=False)
    shares = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    total_value = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    notes = Column(EncryptedText, nullable=True)
    portfolio = relationship("Portfolio", back_populates="transactions")

class ESGExclusion(Base):
    __tablename__ = "esg_exclusions"
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    exclusion_type = Column(String, nullable=False)
    value = Column(String, nullable=False)
    reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    portfolio = relationship("Portfolio", back_populates="esg_exclusions")


class CorporateActionLog(Base):
    """Records every corporate action applied to positions (splits, reverse-splits)."""
    __tablename__ = "corporate_action_logs"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    action_type = Column(String, nullable=False)   # split | reverse_split | dividend
    old_rate = Column(Float, nullable=True)
    new_rate = Column(Float, nullable=True)
    ratio = Column(Float, nullable=False)           # new_rate / old_rate
    ex_date = Column(DateTime, nullable=True)
    processed_at = Column(DateTime, default=datetime.utcnow)
    positions_affected = Column(Integer, default=0)
    notes = Column(Text, nullable=True)



class TradePlan(Base):
    """
    Draft trade plan generated by the TLH advisor (or manually), reviewed and
    approved by the client, reconciled after execution at the customer's broker.
    """
    __tablename__ = "trade_plans"
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False, index=True)
    status = Column(String, nullable=False, default="DRAFT")  # DRAFT|APPROVED|EXECUTED|CANCELLED|EXPIRED
    draft_plan = Column(Text, nullable=True)                  # JSON blob from draft_trade_list()
    summary = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    approved_at = Column(DateTime, nullable=True)
    executed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    recommendation_log_id = Column(Integer, ForeignKey("recommendation_logs.id"), nullable=True)
    items = relationship("TradePlanItem", back_populates="plan", cascade="all, delete-orphan")


class TradePlanItem(Base):
    """A single leg within a TradePlan (BUY or SELL of a symbol)."""
    __tablename__ = "trade_plan_items"
    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("trade_plans.id"), nullable=False, index=True)
    action = Column(String, nullable=False)       # BUY | SELL
    symbol = Column(String, nullable=False)
    shares = Column(Float, nullable=False)
    est_price = Column(Float, nullable=True)
    est_proceeds = Column(Float, nullable=True)
    lot_ids_json = Column(Text, nullable=True)    # JSON-encoded list[int] for SELL legs
    notes = Column(Text, nullable=True)
    plan = relationship("TradePlan", back_populates="items")


class RecommendationLog(Base):
    """
    Immutable record of every AI advisor run. Required by SEC Rule 204-2 for
    RIAs — every personalized recommendation must be reconstructible years later.
    """
    __tablename__ = "recommendation_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False, index=True)
    prompt = Column(EncryptedText, nullable=False)
    model_version = Column(String, nullable=True)
    prompt_version = Column(String, nullable=True)
    tool_calls_json = Column(EncryptedText, nullable=True)   # JSON list of tool_use/tool_result entries
    reasoning = Column(EncryptedText, nullable=True)         # concatenated assistant text blocks
    draft_plan_json = Column(EncryptedText, nullable=True)
    adv_version_acknowledged = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    demo_mode = Column(Boolean, nullable=False, default=False)


class AuditEvent(Base):
    """
    Append-only audit trail: who did what, when, against which object.
    Used for compliance and SEC exam reconstruction.
    """
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event_type = Column(String, nullable=False, index=True)   # e.g. TRADE_PLAN_APPROVED
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=True, index=True)
    object_type = Column(String, nullable=True)               # e.g. trade_plan
    object_id = Column(Integer, nullable=True)
    details_json = Column(EncryptedText, nullable=True)
    ip_address = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class IdempotencyRecord(Base):
    """
    Dedupes POSTs that pass an Idempotency-Key header. Scoped by user+endpoint
    because keys are only expected to be unique per-client. Caller gets the
    cached response body on retry — no double execution.
    """
    __tablename__ = "idempotency_records"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    endpoint = Column(String, nullable=False)
    status_code = Column(Integer, nullable=False, default=200)
    response_body = Column(Text, nullable=True)   # JSON-encoded response
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    __table_args__ = (
        UniqueConstraint("key", "user_id", "endpoint", name="uq_idem_key_user_endpoint"),
    )


class Subscription(Base):
    """
    Stripe-backed subscription state for an individual user. One row per user
    (enforced in service code; DB has an index but not a unique constraint so
    historical rows can survive cancellation without blocking resubscribe).
    """
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    stripe_customer_id = Column(String, nullable=True, index=True)
    stripe_subscription_id = Column(String, nullable=True, index=True)
    tier = Column(String, nullable=False, default="starter")   # starter | standard | premium
    billing_cycle = Column(String, nullable=False, default="monthly")  # monthly | annual
    status = Column(String, nullable=False, default="trialing")
    # trialing | active | past_due | canceled | incomplete | unpaid
    trial_ends_at = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SystemFlag(Base):
    """
    Runtime feature-flag/kill-switch table. The only flag that matters for
    safety today is `trading_halted` — when True, every trade-execution path
    returns 503. Changed via the admin kill-switch endpoint.
    """
    __tablename__ = "system_flags"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)       # "true" / "false" / free text
    reason = Column(Text, nullable=True)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CPAInvite(Base):
    """
    "Invite your CPA" growth-loop record. An individual user shares read-only
    access to their tax report with their CPA. The CPA lands on a limited
    HTML + CSV view (realized gains, Form 8949, lot detail) scoped to one
    portfolio — no login, no authentication beyond the magic-link token.

    Security:
      - Token is a signed JWT (backend/services/cpa_invite_service.py), 30-day
        TTL, one portfolio, one CPA email, one invite row.
      - `revoked_at` kills the link immediately; a new invite issues a new
        token.
      - The view is read-only — no writes are exposed through this path.

    Why it exists: CPAs are natural referrers to RIAs. Collecting CPA emails
    + firm names through this flow is our Phase 2 RIA-channel seed list.
    """
    __tablename__ = "cpa_invites"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False, index=True)
    cpa_email = Column(String, nullable=False)
    cpa_name = Column(String, nullable=True)
    firm_name = Column(String, nullable=True)
    token_hash = Column(String, nullable=False, index=True)  # SHA-256 of the JWT jti
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    first_viewed_at = Column(DateTime, nullable=True)
    last_viewed_at = Column(DateTime, nullable=True)
    view_count = Column(Integer, nullable=False, default=0)
    revoked_at = Column(DateTime, nullable=True)


class IndexConstituent(Base):
    """Persisted constituent rows for tracked indexes."""
    __tablename__ = "index_constituents"
    id = Column(Integer, primary_key=True, index=True)
    index_name = Column(String, nullable=False)  # sp500 | nasdaq100 | russell1000
    symbol = Column(String, nullable=False)
    name = Column(String, nullable=False)
    sector = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    weight = Column(Float, nullable=False, default=0.0)
    market_cap = Column(Float, nullable=True)
    as_of = Column(DateTime, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_index_constituents_index_active", "index_name", "is_active"),
    )


