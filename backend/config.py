from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Market data
    FINNHUB_API_KEY: str
    FINNHUB_BASE_URL: str = "https://finnhub.io/api/v1"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./direct_indexing.db"

    # Portfolio engine
    # Finnhub quote cache TTL in seconds. 5 minutes is the pragmatic default
    # for a tax-harvesting product — the TLH engine decides "is this 5% below
    # basis" not "what's the current bid/ask," so 5-minute-stale prices are
    # invisible and the cache hit rate goes from ~zero to ~95% on typical tab
    # switches. Lower via env to `CACHE_TTL=60` if you need fresher data;
    # higher to 900 (15 min) is fine for low-activity accounts.
    CACHE_TTL: int = 300
    TAX_LOSS_THRESHOLD: float = -0.05
    REBALANCE_THRESHOLD: float = 0.05
    WASH_SALE_DAYS: int = 30

    # Auth
    JWT_SECRET: str
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Anthropic (Claude AI advisor)
    ANTHROPIC_API_KEY: str = ""

    # Model selection per subscription tier. Both default to opus-4-5 so
    # existing behaviour is preserved until you change them. Flip
    # CLAUDE_MODEL_DEFAULT to a cheaper model (e.g. claude-haiku-4-5) after
    # launch to improve margin on starter/standard tiers without touching
    # code or restarting. CLAUDE_MODEL_PREMIUM is used for premium-tier
    # subscribers (subscription.status='active' AND tier='premium'); everyone
    # else (no sub, trialing, starter, standard) gets CLAUDE_MODEL_DEFAULT.
    # When Anthropic deprecates a model, bump these two env vars, run the
    # eval harness, redeploy. See docs/TODO.md M6 for upgrade playbook.
    CLAUDE_MODEL_DEFAULT: str = "claude-opus-4-5"
    CLAUDE_MODEL_PREMIUM: str = "claude-opus-4-5"

    # CORS — comma-separated list of allowed origins
    CORS_ORIGINS: str = "http://localhost:8000"

    # PII column encryption. Read by backend/services/encryption.py directly
    # from os.environ; listed here so pydantic-settings doesn't reject it as
    # an "extra" field when the user puts it in .env.
    FIELD_ENCRYPTION_KEYS: str = ""
    JSON_LOGS: str = "0"

    # SMTP — when unset, verification emails are logged to stdout (dev mode).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "no-reply@directindex.example"
    APP_BASE_URL: str = "http://localhost:8000"

    # Stripe (M8 billing). When empty, /billing/* returns 503.
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    # Price IDs from your Stripe dashboard
    STRIPE_PRICE_STARTER_MONTHLY: str = ""
    STRIPE_PRICE_STARTER_ANNUAL: str = ""
    STRIPE_PRICE_STANDARD_MONTHLY: str = ""
    STRIPE_PRICE_STANDARD_ANNUAL: str = ""
    STRIPE_PRICE_PREMIUM_MONTHLY: str = ""
    STRIPE_PRICE_PREMIUM_ANNUAL: str = ""
    STRIPE_TRIAL_DAYS: int = 14

    # Sentry error tracking. Empty = disabled.
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str = "development"
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.0
    APP_VERSION: str = "1.0.0"

    # AWS (Secrets Manager + S3 backups). Honors standard boto3 env vars
    # (AWS_REGION, AWS_PROFILE, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) too.
    AWS_REGION: str = "us-east-1"
    AWS_SECRETS_PREFIX: str = "direct-indexing/"
    S3_BACKUP_BUCKET: str = ""
    S3_BACKUP_PREFIX: str = "pgdumps/"
    S3_BACKUP_RETENTION_DAYS: int = 30

    # TOTP admin MFA
    TOTP_ISSUER: str = "DirectIndex Pro"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
