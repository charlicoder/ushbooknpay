"""
app/core/config.py
──────────────────
Centralised, typed application settings powered by Pydantic Settings.

All values come from environment variables (or .env file).
No defaults are provided for secrets — the application will refuse to start
if required secrets are absent.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Group order reflects dependency layers:
      1. Identity / runtime
      2. Server
      3. Database
      4. Redis
      5. Security
      6. API Gateway / inter-service
      7. AWS
      8. Payment providers
      9. Domain configuration
      10. Operational tunables
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 1. Identity ──────────────────────────────────────────────────────
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_DEBUG: bool = False
    APP_NAME: str = "ushbooknpay"
    APP_VERSION: str = "1.0.0"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ── 2. Server ────────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8001, ge=1, le=65535)
    WORKERS: int = Field(default=1, ge=1)

    # ── 3. Database ──────────────────────────────────────────────────────
    DATABASE_URL: str  # must start with postgresql+asyncpg://
    DATABASE_POOL_SIZE: int = Field(default=10, ge=1, le=100)
    DATABASE_MAX_OVERFLOW: int = Field(default=20, ge=0)
    DATABASE_POOL_TIMEOUT: int = Field(default=30, ge=5)

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the 'postgresql+asyncpg://' scheme for async support."
            )
        return v

    # ── 4. Redis ─────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_MAX_CONNECTIONS: int = Field(default=20, ge=1)
    REDIS_SOCKET_TIMEOUT: int = Field(default=5, ge=1)

    # ── 5. Security ──────────────────────────────────────────────────────
    USHSPA_TOKEN: str = ""  # required — never log
    USH_TOKEN: str | None = None  # alias for USHSPA_TOKEN
    JWT_PUBLIC_KEY: str = ""  # optional when using interservice auth
    JWT_ALGORITHM: str = "RS256"
    JWT_AUDIENCE: str = "ushspa"
    JWT_ISSUER: str = "ushauth"

    @model_validator(mode="after")
    def validate_app_tokens(self) -> Settings:
        if not self.USHSPA_TOKEN and self.USH_TOKEN:
            self.USHSPA_TOKEN = self.USH_TOKEN
        elif not self.USH_TOKEN and self.USHSPA_TOKEN:
            self.USH_TOKEN = self.USHSPA_TOKEN
        if not self.USHSPA_TOKEN:
            raise ValueError("USHSPA_TOKEN or USH_TOKEN must be configured.")
        return self

    @field_validator("JWT_PUBLIC_KEY")
    @classmethod
    def validate_jwt_public_key(cls, v: str) -> str:
        # Replace literal \n with real newlines when loaded from env
        return v.replace("\\n", "\n") if v else ""

    # ── 6. API Gateway / inter-service ──────────────────────────────────
    API_GATEWAY_BASE_URL: str = "http://api.ushspa.local"
    USHAUTH_BASE_PATH: str = "/uauth"
    USHBOOKNPAY_BASE_PATH: str = "/booknpay"
    USHNOTICE_BASE_PATH: str = "/unotice"
    GATEWAY_TIMEOUT: int = Field(default=10, ge=1, le=60)

    # Direct inter-service URL for ushauth (bypasses the API gateway).
    # Set this to the ushauth container/host URL (e.g. http://host.docker.internal:8002).
    # When set, all inter-service calls to ushauth use this URL directly instead of
    # routing through the API gateway (API_GATEWAY_BASE_URL + USHAUTH_BASE_PATH).
    USHAUTH_DIRECT_URL: str = ""

    @property
    def ushauth_base_url(self) -> str:
        """Full base URL for ushauth inter-service calls.

        Prefers USHAUTH_DIRECT_URL (direct service-to-service, no gateway prefix)
        over the gateway URL to avoid double-auth layers at the gateway.
        """
        if self.USHAUTH_DIRECT_URL:
            return self.USHAUTH_DIRECT_URL.rstrip("/")
        return f"{self.API_GATEWAY_BASE_URL.rstrip('/')}{self.USHAUTH_BASE_PATH}"

    # ── 7. AWS ───────────────────────────────────────────────────────────
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""  # never log
    AWS_REGION: str = "ap-south-1"
    AWS_SESSION_TOKEN: str = ""  # optional temporary credentials

    AWS_SQS_NOTIFICATION_QUEUE_URL: str = ""
    AWS_S3_BUCKET: str = ""

    @field_validator(
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_SESSION_TOKEN",
        "AWS_SQS_NOTIFICATION_QUEUE_URL",
        "AWS_S3_BUCKET",
        mode="before",
    )
    @classmethod
    def sanitize_aws_fields(cls, v: object) -> str:
        if isinstance(v, str):
            # Strip inline comments e.g. "value # comment" -> "value"
            return v.split("#")[0].strip()
        return str(v) if v is not None else ""

    @model_validator(mode="after")
    def validate_aws_in_production(self) -> "Settings":
        if self.APP_ENV == "production":
            missing = []
            if not self.AWS_ACCESS_KEY_ID:
                missing.append("AWS_ACCESS_KEY_ID")
            if not self.AWS_SECRET_ACCESS_KEY:
                missing.append("AWS_SECRET_ACCESS_KEY")
            if not self.AWS_SQS_NOTIFICATION_QUEUE_URL:
                missing.append("AWS_SQS_NOTIFICATION_QUEUE_URL")
            if missing:
                raise ValueError(
                    f"Required AWS environment variables missing in production: {missing}"
                )
        return self

    # ── 8. Payment Providers ─────────────────────────────────────────────
    # MyFatoorah
    MYFATOORAH_API_KEY: str = ""  # never log
    MYFATOORAH_BASE_URL: str = "https://apitest.myfatoorah.com"
    MYFATOORAH_CALLBACK_URL: str = ""
    MYFATOORAH_SUCCESS_URL: str = ""
    MYFATOORAH_ERROR_URL: str = ""

    # Tap Payments
    TAP_SECRET_KEY: str = ""  # never log
    TAP_BASE_URL: str = "https://api.tap.company/v2"
    TAP_CALLBACK_URL: str = ""
    TAP_REDIRECT_URL: str = ""

    # ── 9. Domain Configuration ──────────────────────────────────────────
    AVAILABILITY_DAYS_AHEAD: int = Field(default=10, ge=1, le=60)
    HOME_SERVICE_BUFFER_MINUTES: int = Field(default=30, ge=0, le=120)
    TEMPORARY_HOLD_MINUTES: int = Field(default=15, ge=5, le=60)
    SLOT_DURATION_MINUTES: int = Field(default=30, ge=15, le=60)
    LOYALTY_REWARD_EXPIRY_DAYS: int = Field(
        default=60,
        ge=1,
        le=365,
        description="Number of days after creation that a loyalty reward expires.",
    )
    GIFT_VOUCHER_EXPIRE_DAYS: int = Field(
        default=60,
        ge=1,
        le=730,
        description="Number of days after creation that a gift voucher expires.",
    )

    # ── 10. Operational Tunables ─────────────────────────────────────────
    RATE_LIMIT_REQUESTS: int = Field(default=100, ge=1)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1)
    IDEMPOTENCY_TTL_SECONDS: int = Field(default=86400, ge=60)
    CATALOG_CACHE_TTL_SECONDS: int = Field(default=300, ge=10)

    # ── Convenience ──────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the cached settings singleton.

    Use this as a FastAPI dependency::

        from app.core.config import get_settings
        settings = Depends(get_settings)

    Or import directly for use outside request context::

        from app.core.config import get_settings
        settings = get_settings()
    """
    return Settings()
