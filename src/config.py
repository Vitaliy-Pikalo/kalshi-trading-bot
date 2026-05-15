"""central config — loads from .env via pydantic-settings.

every setting has a sane default + validation. import `settings` anywhere:

    from src.config import settings
    print(settings.kalshi_env)
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- kalshi auth ---
    kalshi_env: Literal["demo", "prod"] = "demo"
    kalshi_key_id: str = Field(default="", description="UUID from Kalshi settings")
    kalshi_private_key_path: str = Field(
        default="", description="Absolute path to RSA .pem file"
    )
    kalshi_api_base: str | None = Field(default=None)

    # --- db ---
    database_url: str = "sqlite:///data/poker_quant.db"

    # --- bankroll / risk (poker-style) ---
    bankroll_usd: float = 100.0
    max_risk_per_trade_pct: float = 1.0
    kelly_fraction: float = 0.25
    min_edge_pct: float = 3.0
    daily_loss_limit_pct: float = 5.0
    drawdown_limit_pct: float = 20.0

    # --- logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- derived ---
    @property
    def api_base_url(self) -> str:
        """Resolve API base from env or override."""
        if self.kalshi_api_base:
            return self.kalshi_api_base
        if self.kalshi_env == "demo":
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"

    @field_validator("kalshi_private_key_path")
    @classmethod
    def _check_key_path(cls, v: str) -> str:
        if v and not Path(v).exists():
            # warn but don't fail at import time — user may not have created key yet
            import warnings

            warnings.warn(
                f"kalshi_private_key_path does not exist: {v}",
                stacklevel=2,
            )
        return v

    @field_validator("kelly_fraction")
    @classmethod
    def _kelly_sane(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError(f"kelly_fraction must be in (0, 1], got {v}")
        if v > 0.5:
            import warnings

            warnings.warn(
                f"kelly_fraction={v} is aggressive. quarter-kelly (0.25) is poker standard.",
                stacklevel=2,
            )
        return v


settings = Settings()


if __name__ == "__main__":
    # quick sanity check: python -m src.config
    print(f"env: {settings.kalshi_env}")
    print(f"api: {settings.api_base_url}")
    print(f"bankroll: ${settings.bankroll_usd}")
    print(f"max risk per trade: {settings.max_risk_per_trade_pct}%")
    print(f"kelly fraction: {settings.kelly_fraction}")
    print(f"min edge: {settings.min_edge_pct}%")
    print(f"key id set: {bool(settings.kalshi_key_id)}")
    print(f"key path: {settings.kalshi_private_key_path or '(not set)'}")
