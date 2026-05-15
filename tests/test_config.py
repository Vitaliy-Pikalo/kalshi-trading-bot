"""sanity tests that don't need network or kalshi creds."""
from __future__ import annotations

import pytest


def test_config_loads():
    from src.config import settings
    assert settings.kalshi_env in ("demo", "prod")
    assert settings.api_base_url.startswith("https://")
    assert 0 < settings.kelly_fraction <= 1
    assert settings.bankroll_usd > 0


def test_kelly_fraction_validation():
    from pydantic import ValidationError

    from src.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings(kelly_fraction=0)
    with pytest.raises((ValidationError, ValueError)):
        Settings(kelly_fraction=1.5)


def test_api_base_switches_on_env():
    from src.config import Settings

    demo = Settings(kalshi_env="demo")
    prod = Settings(kalshi_env="prod")
    assert "demo" in demo.api_base_url
    assert "demo" not in prod.api_base_url
