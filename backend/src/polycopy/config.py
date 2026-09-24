"""Environment-based configuration with fail-closed validation.

Safety model (corrected after review):

* ``order_kill_switch`` is an INDEPENDENT global execution gate. When ON it
  blocks ALL order creation — paper and live. It is deliberately allowed in
  combination with live mode: the safe way to boot a live-capable system is
  with the kill switch ON (connect, reconcile, observe — execute nothing).
  Clearing the kill switch is an explicit, separate operator action.
* ``allow_live_trading`` only controls which execution broker implementation
  the system loads (PaperExecutionBroker vs LiveExecutionBroker, Chunk 2/3).
* Private key material while ``allow_live_trading`` is False remains a hard
  startup error. There is no "default allow" path anywhere in the system.
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
    # Global execution gate: blocks ALL order creation (paper AND live).
    # Defaults ON. Live-capable boot with kill switch ON is the intended
    # staging posture: connect and reconcile, execute nothing.
    order_kill_switch: bool = True

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
    # V1 paper sizing: fixed USD per signal (TK decision, 2026-09-20).
    # Evidence on every paper order (book snapshot, depth) exists so larger
    # sizing can be evaluated later from real detection-time data.
    max_order_size_usd: float = 10.0
    max_exposure_per_market_usd: float = 100.0
    max_exposure_global_usd: float = 500.0
    # A signal is not eligible to execute until t1_detected_at + this many
    # seconds. Detection/decision evidence reflects the configured delay.
    review_delay_seconds: float = 30.0
    # Source-trade t0 to paper decision; prevents old kill-switch backlogs
    # from executing against a much later order book.
    max_signal_execution_age_seconds: float = 300.0
    # Per-cycle bounds (PR #7 hardening): one cycle never exceeds this many
    # new signals / book requests, so a backlogged DB can never turn into
    # an unbounded execution storm (the VPS OOM lesson).
    signal_detection_batch_size: int = 200
    execution_batch_size: int = 50

    # --- Paper execution --------------------------------------------------
    # Fee rate applied to paper fill notional (0.01 = 1%). Config, not a
    # constant: if Polymarket's fee model changes, this changes with it.
    paper_fee_rate: float = 0.0
    # Entries at this price or higher are skipped (~10% max upside is not
    # worth fees + slippage — docs/wallet-intelligence.md §5).
    max_copy_price: float = 0.90

    # --- Ingestion bounds (Chunk 2; defined now so nobody forgets) -------
    # One newest page per wallet per cycle; approved wallets have a separate,
    # persisted, per-cycle bounded continuity recovery when the anchor is lost.
    ingestion_batch_size: int = 500
    ingestion_max_concurrent_requests: int = 4
    ingestion_poll_interval_seconds: float = 15.0
    catch_up_pages_per_cycle: int = 3
    # Candidate-only history bootstrap. These cumulative bounds are separate
    # from the recurring live-tail batch above.
    bootstrap_page_size: int = 100
    bootstrap_max_pages: int = 200
    bootstrap_max_trades: int = 20000
    bootstrap_max_requests: int = 240
    bootstrap_pages_per_run: int = 25
    bootstrap_max_settlement_markets: int = 20
    settlement_max_checks_per_cycle: int = 10
    candidate_scoring_interval_seconds: int = 3600

    @field_validator("environment")
    @classmethod
    def _env_name(cls, v: str) -> str:
        allowed = {"development", "staging", "production", "test"}
        if v not in allowed:
            raise ValueError(f"environment must be one of {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _fail_closed(self) -> Settings:
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
        # NOTE: allow_live_trading + order_kill_switch=True is INTENTIONALLY
        # ALLOWED — it is the safe staging posture for a live-capable system.
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
