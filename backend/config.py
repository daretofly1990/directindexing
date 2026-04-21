from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Market data
    FINNHUB_API_KEY: str
    FINNHUB_BASE_URL: str = "https://finnhub.io/api/v1"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./direct_indexing.db"

    # Portfolio engine
    CACHE_TTL: int = 60
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
