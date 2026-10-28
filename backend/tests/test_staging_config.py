"""Staging must never boot with production money keys.

`render.staging.yaml` runs the production config gates on purpose, so
`APP_ENV=production` alone cannot tell the app "this must never move real
money". The blueprint sets `STAGING_DEPLOYMENT=true`, and this gate is what
turns a copy-paste mistake into a failed deploy instead of a real charge or a
real transfer.
"""

from __future__ import annotations

import pytest

GOOD_JWT = "this-is-a-32-char-secret-for-testing-only!!"

KEYS = ("PAYSTACK_SECRET_KEY", "TCDN_AI_PAYSTACK_SECRET_KEY")


def _fresh_settings(monkeypatch, **env: str):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", GOOD_JWT)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    from app.config import get_settings

    get_settings.cache_clear()
    return get_settings


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.delenv("STAGING_DEPLOYMENT", raising=False)
    for name in KEYS:
        monkeypatch.setenv(name, "")
    yield
    from app.config import get_settings

    get_settings.cache_clear()


class TestStagingMoneyGuard:
    def test_cdn_billing_live_key_refuses_to_boot(self, monkeypatch, clean):
        get_settings = _fresh_settings(
            monkeypatch,
            STAGING_DEPLOYMENT="true",
            PAYSTACK_SECRET_KEY="sk_live_abc123",
        )
        with pytest.raises(ValueError, match="staging must use sk_test_ keys"):
            get_settings()
        get_settings.cache_clear()

    def test_ai_payment_live_key_refuses_to_boot(self, monkeypatch, clean):
        get_settings = _fresh_settings(
            monkeypatch,
            STAGING_DEPLOYMENT="true",
            TCDN_AI_PAYSTACK_SECRET_KEY="sk_live_abc123",
        )
        with pytest.raises(ValueError, match="TCDN_AI_PAYSTACK_SECRET_KEY"):
            get_settings()
        get_settings.cache_clear()

    def test_test_keys_are_accepted(self, monkeypatch, clean):
        get_settings = _fresh_settings(
            monkeypatch,
            STAGING_DEPLOYMENT="true",
            PAYSTACK_SECRET_KEY="sk_test_abc123",
            TCDN_AI_PAYSTACK_SECRET_KEY="sk_test_def456",
        )
        settings = get_settings()
        assert settings.paystack_secret_key == "sk_test_abc123"
        get_settings.cache_clear()

    def test_production_may_still_use_a_live_key(self, monkeypatch, clean):
        # The guard is scoped to staging: production must keep working.
        get_settings = _fresh_settings(
            monkeypatch,
            PAYSTACK_SECRET_KEY="sk_live_abc123",
        )
        settings = get_settings()
        assert settings.paystack_secret_key == "sk_live_abc123"
        get_settings.cache_clear()

    def test_flag_accepts_the_usual_spellings(self, monkeypatch, clean):
        for value in ("1", "TRUE", "yes"):
            get_settings = _fresh_settings(
                monkeypatch,
                STAGING_DEPLOYMENT=value,
                PAYSTACK_SECRET_KEY="sk_live_abc123",
            )
            with pytest.raises(ValueError, match="staging must use sk_test_ keys"):
                get_settings()
            get_settings.cache_clear()
