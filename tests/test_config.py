"""Configuration rules that must survive the port (§11)."""

from __future__ import annotations

import pytest
from todd.config import DEV_MASTER_KEY, Settings


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type,call-arg]


def test_dev_bypass_is_refused_in_production() -> None:
    """§8.17 — it makes every request the dev user."""
    with pytest.raises(ValueError, match="AUTH_DEV_BYPASS"):
        _settings(app_env="production", auth_dev_bypass=True)


def test_dev_bypass_is_allowed_in_development() -> None:
    assert _settings(app_env="development", auth_dev_bypass=True).auth_dev_bypass


def test_upgrade_lock_mode_requires_a_price() -> None:
    with pytest.raises(ValueError, match="PRO_PRICE_PAISE"):
        _settings(pro_lock_mode="upgrade")
    assert _settings(pro_lock_mode="upgrade", pro_price_paise=19900).pro_price_paise == 19900


def test_missing_master_key_falls_back_to_a_fixed_constant() -> None:
    """§8.16 — a per-process random key makes every sealed secret unreadable
    by every other process, which presents as data corruption."""
    first = _settings(encryption_master_key=None)
    second = _settings(encryption_master_key=None)
    assert first.encryption_master_key == second.encryption_master_key == DEV_MASTER_KEY


def test_missing_master_key_is_fatal_in_production() -> None:
    with pytest.raises(ValueError, match="ENCRYPTION_MASTER_KEY"):
        _settings(
            app_env="production",
            encryption_master_key=None,
            supabase_url="x",
            supabase_service_role_key="x",
            supabase_jwt_secret="x",
            google_client_id="x",
            google_client_secret="x",
        )


def test_a_blank_env_value_means_unset() -> None:
    """`.env.example` ships every key empty; copied to `.env`, a bare
    `PRO_PRICE_PAISE=` must not stop the service booting."""
    assert _settings(pro_price_paise="", llm_daily_cost_cap_micros="").pro_price_paise is None


def test_web_origin_accepts_a_list() -> None:
    s = _settings(web_origin="http://localhost:3000, https://todd.app")
    assert s.cors_origins == ["http://localhost:3000", "https://todd.app"]
