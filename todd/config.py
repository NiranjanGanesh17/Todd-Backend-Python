"""Settings, loaded once at boot and validated as a whole.

Every entry point — API, worker, scripts — imports `get_settings()` rather than
reading `os.environ`. The seed script in the Node version read the environment
raw and died with "DATABASE_URL is not set" while it sat in `.env`.

Bad *combinations* raise here, at import, instead of failing at first use.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from todd.schemas.enums import LockMode

AppEnv = Literal["development", "test", "production"]

#: Used when ENCRYPTION_MASTER_KEY is unset outside production. It must be a
#: constant: a per-process random key means a secret sealed by one process is
#: unreadable by every other, which looks like data corruption (§8.16).
#:
#: It must also decode to exactly 32 bytes — SecretBox.KEY_SIZE. The first
#: version of this constant decoded to 35, and because every crypto test built
#: its own key with `generate_master_key()`, nothing caught it until a live
#: OAuth callback tried to seal a refresh token. `test_crypto.py` now exercises
#: this value directly.
DEV_MASTER_KEY = "dG9kZC1kZXZlbG9wbWVudC1vbmx5LW1hc3Rlci1rZXk="

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- app
    app_env: AppEnv = "development"
    log_level: str = "info"
    port: int = 4000
    web_origin: str = "http://localhost:3000"
    service_name: str = "todd-api"
    version: str = "0.1.0"

    # ---- database
    database_url: str = "postgresql+psycopg://todd:todd@localhost:5433/todd_py"
    database_ssl: bool = False
    database_pool_max: int = 10

    # ---- redis
    redis_url: str = "redis://localhost:6379/0"

    # ---- supabase
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_jwt_secret: str | None = None
    supabase_storage_bucket: str = "statements"

    # ---- google / gmail
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_oauth_redirect_uri: str = "http://localhost:4000/v1/gmail/callback"
    gmail_pubsub_topic: str | None = None
    gmail_pubsub_verification_token: str | None = None
    gmail_rate_limit_per_minute: int = 60

    # ---- anthropic
    anthropic_api_key: str | None = None
    #: Rung 8 is a bulk classifier over short strings, so it runs on the fast
    #: model. Statement extraction (§7.4) is the job for the smart one.
    anthropic_model_fast: str | None = "claude-haiku-5-5"
    anthropic_model_smart: str | None = None
    llm_daily_cost_cap_micros: int | None = 50_000  # $0.05/user/day
    #: §9 rung 8. Off by default: it is the only rung that spends money, and
    #: it must not start doing so because a config file was copied.
    llm_enrich_enabled: bool = False

    # ---- encryption
    encryption_master_key: str | None = None
    encryption_key_id: str = "dev-1"

    # ---- web push
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str | None = None

    # ---- pro / goals
    goals_enabled: bool = False
    pro_lock_mode: LockMode = LockMode.WAITLIST
    pro_price_paise: int | None = None

    # ---- retention / observability
    raw_body_retention_days: int = 30
    sentry_dsn: str | None = None

    # ---- dev auth bypass
    auth_dev_bypass: bool = False
    auth_dev_user_id: str = "00000000-0000-4000-8000-000000000001"
    auth_dev_user_email: str = "dev@todd.local"

    # ------------------------------------------------------------------ rules

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        """A key present in `.env` with no value is not set.

        `.env.example` ships every key with an empty value so the file documents
        the full surface. Copied to `.env`, a bare `PRO_PRICE_PAISE=` would
        otherwise fail integer parsing and stop the service booting.
        """
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and v.strip() == "")}
        return data

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _dev_bypass_never_in_production(self) -> Settings:
        """AUTH_DEV_BYPASS makes every request the dev user. In production that
        is a total authentication bypass, so refuse to boot rather than serve."""
        if self.auth_dev_bypass and self.is_production:
            raise ValueError(
                "AUTH_DEV_BYPASS=true with APP_ENV=production. This makes every "
                "request the dev user. Refusing to start."
            )
        return self

    @model_validator(mode="after")
    def _upgrade_mode_needs_a_price(self) -> Settings:
        """A paywall that cannot name its price renders a blank button."""
        if self.pro_lock_mode is LockMode.UPGRADE and self.pro_price_paise is None:
            raise ValueError("PRO_LOCK_MODE=upgrade requires PRO_PRICE_PAISE.")
        return self

    @model_validator(mode="after")
    def _master_key_present(self) -> Settings:
        """Required in production; a fixed, logged constant everywhere else."""
        if self.encryption_master_key:
            return self
        if self.is_production:
            raise ValueError("ENCRYPTION_MASTER_KEY is required when APP_ENV=production.")
        object.__setattr__(self, "encryption_master_key", DEV_MASTER_KEY)
        logger.warning(
            "ENCRYPTION_MASTER_KEY unset — using the fixed development key. "
            "Secrets sealed with it are not confidential."
        )
        return self

    @model_validator(mode="after")
    def _production_needs_its_integrations(self) -> Settings:
        if not self.is_production:
            return self
        missing = [
            name
            for name, value in (
                ("SUPABASE_URL", self.supabase_url),
                ("SUPABASE_SERVICE_ROLE_KEY", self.supabase_service_role_key),
                ("SUPABASE_JWT_SECRET", self.supabase_jwt_secret),
                ("GOOGLE_CLIENT_ID", self.google_client_id),
                ("GOOGLE_CLIENT_SECRET", self.google_client_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"APP_ENV=production requires: {', '.join(missing)}.")
        return self

    @property
    def cors_origins(self) -> list[str]:
        """Comma-separated WEB_ORIGIN, so staging and local can both be allowed.

        Never `*`: the browser sends credentials, and the two are incompatible.
        """
        return [o.strip() for o in self.web_origin.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the whole process shares one validated object."""
    return Settings()


# Re-exported for tests that need to build a Settings with overrides.
__all__ = ["DEV_MASTER_KEY", "Settings", "get_settings"]
