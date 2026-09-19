"""Environment-based configuration with fail-closed validation.

Safety rules enforced here (imported from Polycopy v1's philosophy):

* Paper mode is the default and live trading is OFF unless explicitly enabled.
* If ``allow_live_trading`` is False (the default), the presence of any
  private key material in the environment is a hard config error.
* There is no "default allow" path anywhere in the system.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLYCOPY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core mode flags -------------------------------------------------
    environment: str = "development"
    paper_mode: bool = True
    allow_live_trading: bool = False
    order_kill_switch: bool = True  # defaults ON: nothing may trade until explicitly cleared

    # --- Secrets (must stay empty while allow_live_trading is False) -----
    polymarket_private_key: str = ""

    # --- Database / cache ------------------------------------------------
    database_url: str = "postgresql+asyncpg://polycopy:polycopy@postgres:5432/polycopy"
    redis_url: str = "redis://redis:6379/0"
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # --- API -------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    # --- Risk limits (enforced by the bot in Chunk 2; surfaced now) ------
    max_order_size_usd: float = 25.0
    max_exposure_per_market_usd: float = 100.0
    max_exposure_global_usd: float = 500.0
    review_delay_seconds: float = 30.0

    # --- Ingestion bounds (Chunk 2; defined now so nobody forgets) -------
    ingestion_batch_size: int = 500
    ingestion_max_concurrent_requests: int = 4
    ingestion_poll_interval_seconds: float = 15.0

    @field_validator("environment")
    @classmethod
    def _env_name(cls, v: str) -> str:
        allowed = {"development", "staging", "production", "test"}
        if v not in allowed:
            raise ValueError(f"environment must be one of {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _fail_closed(self) -> "Settings":
        """Reject unsafe combinations. Fail-closed: doubt means error."""
        if not self.allow_live_trading and self.polymarket_private_key:
            raise ValueError(
                "POLYCOPY_POLYMARKET_PRIVATE_KEY is set but "
                "POLYCOPY_ALLOW_LIVE_TRADING is false. Refusing to start: "
                "private keys are forbidden outside of explicitly enabled live mode."
            )
        if self.allow_live_trading and self.paper_mode:
            raise ValueError(
                "POLYCOPY_ALLOW_LIVE_TRADING=true and POLYCOPY_PAPER_MODE=true "
                "is ambiguous. Set POLYCOPY_PAPER_MODE=false to run live."
            )
        if self.order_kill_switch and self.allow_live_trading:
            raise ValueError(
                "POLYCOPY_ORDER_KILL_SWITCH=true blocks all order creation; "
                "combining it with live trading is almost certainly a mistake."
            )
        return self

    def public_dict(self) -> dict:
        """Config view safe to expose via API: no secrets, ever."""
        data = self.model_dump()
        for key in list(data):
            if "key" in key or "secret" in key or "password" in key:
                data[key] = "***" if data[key] else ""
        data["database_url"] = data["database_url"].split("@")[-1]  # host/db only
        return data


@lru_cache
def get_settings() -> Settings:
    return Settings()
