"""
Unit tests for the security hardening behavior:

1. POST /auth/setup gating (ADMIN_SETUP_ENABLED bootstrap mode)
2. IP+email login lockout (Redis primary, in-memory fallback)
3. Fail-closed JWT configuration (rejects 'changeme' / short secrets)
4. In-memory rate-limit fallbacks (auth login/forgot, files public, analytics, global middleware)
"""
import time
from io import BytesIO
from unittest.mock import AsyncMock
from uuid import UUID

import pytest


def _mock_bcrypt_user(mock_conn, email="user@example.com", password="securePass123"):
    """Set mock_conn.fetchrow to a bcrypt-hashed user that passes password verification."""
    from passlib.context import CryptContext
    pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
    mock_conn.fetchrow.return_value = {
        "id": UUID("11111111-1111-4111-8111-111111111111"),
        "email": email,
        "password_hash": pwd.hash(password),
        "verified": True,
    }


class TestAuthSetupGating:
    """POST /auth/setup must only promote admins while ADMIN_SETUP_ENABLED=true."""

    URL = "/auth/setup"

    @staticmethod
    def _enable_setup(monkeypatch, enabled: bool):
        """Patch the route's get_settings to return a fake settings with admin_setup_enabled."""
        import app.routes.auth

        class _FakeSettings:
            admin_setup_enabled = enabled

        monkeypatch.setattr(app.routes.auth, "get_settings", lambda: _FakeSettings())

    def test_setup_disabled_returns_403(self, client, auth_headers, monkeypatch):
        """With ADMIN_SETUP_ENABLED unset/false, /auth/setup is locked down."""
        self._enable_setup(monkeypatch, enabled=False)
        resp = client.post(self.URL, headers=auth_headers)
        assert resp.status_code == 403
        assert "Admin setup is disabled" in resp.json()["detail"]

    def test_setup_requires_auth(self, client, monkeypatch):
        """Even with setup enabled, an unauthenticated request gets 401."""
        self._enable_setup(monkeypatch, enabled=True)
        resp = client.post(self.URL)
        assert resp.status_code == 401

    def test_setup_promotes_user_by_email_when_no_admin_exists(self, client, auth_headers, mock_conn, monkeypatch):
        """Bootstrap mode: no admin yet → requesting email gets promoted."""
        self._enable_setup(monkeypatch, enabled=True)
        mock_conn.fetchval.return_value = False  # no admin exists
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "newadmin@example.com",
            "is_admin": False,
        }
        resp = client.post(self.URL, headers=auth_headers, params={"email": "newadmin@example.com"})
        assert resp.status_code == 200
        assert "promoted to admin" in resp.json()["detail"]
        mock_conn.execute.assert_called_once()

    def test_setup_no_email_promotes_first_user(self, client, auth_headers, mock_conn, monkeypatch):
        """Bootstrap mode without email → promotes the first registered user."""
        self._enable_setup(monkeypatch, enabled=True)
        mock_conn.fetchval.side_effect = [
            False,  # no admin exists
            UUID("11111111-1111-4111-8111-111111111111"),  # first user id
        ]
        resp = client.post(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        assert "First user promoted to admin" in resp.json()["detail"]

    def test_setup_admin_exists_non_admin_blocked(self, client, auth_headers, mock_conn, monkeypatch):
        """Once an admin exists, a non-admin requester cannot promote anyone."""
        self._enable_setup(monkeypatch, enabled=True)
        mock_conn.fetchval.return_value = True  # admin exists
        mock_conn.fetchrow.return_value = {"is_admin": False}  # requester is not admin
        resp = client.post(self.URL, headers=auth_headers, params={"email": "someone@example.com"})
        assert resp.status_code == 403
        assert "An admin account already exists" in resp.json()["detail"]
        mock_conn.execute.assert_not_called()  # no promotion may happen

    def test_setup_admin_can_promote_another_user(self, client, admin_headers, mock_conn, monkeypatch):
        """An existing admin can promote additional users."""
        self._enable_setup(monkeypatch, enabled=True)
        mock_conn.fetchval.return_value = True  # admin exists
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # requester IS admin
            {"id": UUID("33333333-3333-4333-8333-333333333333"), "email": "target@example.com", "is_admin": False},
        ]
        resp = client.post(self.URL, headers=admin_headers, params={"email": "target@example.com"})
        assert resp.status_code == 200
        assert "promoted to admin" in resp.json()["detail"]

    def test_setup_email_not_found(self, client, auth_headers, mock_conn, monkeypatch):
        """Requesting promotion for an unknown email is a no-op (no error)."""
        self._enable_setup(monkeypatch, enabled=True)
        mock_conn.fetchval.return_value = False
        mock_conn.fetchrow.return_value = None
        resp = client.post(self.URL, headers=auth_headers, params={"email": "ghost@example.com"})
        assert resp.status_code == 200
        assert "No user found" in resp.json()["detail"]

    def test_setup_email_already_admin(self, client, auth_headers, mock_conn, monkeypatch):
        """Promoting an existing admin is idempotent (no error)."""
        self._enable_setup(monkeypatch, enabled=True)
        mock_conn.fetchval.return_value = False
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "admin@example.com",
            "is_admin": True,
        }
        resp = client.post(self.URL, headers=auth_headers, params={"email": "admin@example.com"})
        assert resp.status_code == 200
        assert "already an admin" in resp.json()["detail"]


class TestLoginLockout:
    """Login rate limiting is keyed by IP + email (attacker can't lock out a victim)."""

    LOGIN_URL = "/auth/login"

    def test_lockout_key_scoped_by_email(self, client, mock_conn, mock_redis):
        """Flooding attacker@example.com from an IP must NOT block user@example.com on the same IP."""
        def fake_get(key):
            if key == "login_attempts:testclient:attacker@example.com":
                return "5"  # attacker already at the limit
            return None

        mock_redis.get.side_effect = fake_get

        # Attacker is blocked
        resp = client.post(self.LOGIN_URL, json={"email": "attacker@example.com", "password": "x"})
        assert resp.status_code == 429

        # Victim on the SAME IP still logs in fine (different email = different key)
        _mock_bcrypt_user(mock_conn, email="user@example.com")
        resp = client.post(self.LOGIN_URL, json={"email": "user@example.com", "password": "securePass123"})
        assert resp.status_code == 200

    def test_lockout_after_five_failed_attempts(self, client, mock_conn, mock_redis):
        """5 failed attempts for the same IP+email → 6th attempt is 429."""
        rate_key = "login_attempts:testclient:user@example.com"

        # 4 failures already recorded → this attempt is the 5th → 401 (counter goes to 5)
        mock_redis.get.side_effect = lambda key: "4" if key == rate_key else None
        resp = client.post(self.LOGIN_URL, json={"email": "user@example.com", "password": "wrongPass1"})
        assert resp.status_code == 401

        # Counter now at 5 → blocked
        mock_redis.get.side_effect = lambda key: "5" if key == rate_key else None
        resp = client.post(self.LOGIN_URL, json={"email": "user@example.com", "password": "wrongPass1"})
        assert resp.status_code == 429
        assert "Too many login attempts" in resp.json()["detail"]

    def test_rate_key_includes_ip_and_email(self, client, mock_conn, mock_redis):
        """The rate-limit key must be `login_attempts:{ip}:{email}`."""
        resp = client.post(self.LOGIN_URL, json={"email": "user@example.com", "password": "wrongPass1"})
        assert resp.status_code == 401  # mock_conn has no user → invalid creds

        mock_redis.get.assert_any_call("login_attempts:testclient:user@example.com")
        mock_redis.incr.assert_any_call("login_attempts:testclient:user@example.com")

    def test_successful_login_resets_counter(self, client, mock_conn, mock_redis):
        """A successful login clears the failed-attempt counter."""
        _mock_bcrypt_user(mock_conn)
        rate_key = "login_attempts:testclient:user@example.com"
        mock_redis.get.side_effect = lambda key: "4" if key == rate_key else None

        resp = client.post(self.LOGIN_URL, json={"email": "user@example.com", "password": "securePass123"})
        assert resp.status_code == 200
        mock_redis.delete.assert_any_call(rate_key)

    def test_lockout_keyed_on_xff_client_ip(self, client, mock_conn, mock_redis):
        """Behind a proxy, X-Forwarded-For (real client) drives the lockout key,
        not the proxy's varying edge IP (request.client.host)."""
        rate_key = "login_attempts:203.0.113.9:user@example.com"
        mock_redis.get.side_effect = lambda key: "5" if key == rate_key else None

        resp = client.post(
            self.LOGIN_URL,
            json={"email": "user@example.com", "password": "wrongPass1"},
            headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        )
        assert resp.status_code == 429
        assert "Too many login attempts" in resp.json()["detail"]

    def test_lockout_ignores_forged_xff(self, client, mock_conn, mock_redis):
        """A malformed X-Forwarded-For (e.g. attacker junk) falls back to the peer IP."""
        rate_key = "login_attempts:testclient:user@example.com"
        mock_redis.get.side_effect = lambda key: "5" if key == rate_key else None

        resp = client.post(
            self.LOGIN_URL,
            json={"email": "user@example.com", "password": "wrongPass1"},
            headers={"X-Forwarded-For": "not-an-ip<script>"},
        )
        assert resp.status_code == 429  # keyed on the peer (testclient), not the junk


class TestGetClientIp:
    """get_client_ip() must prefer the first X-Forwarded-For entry (real client
    behind Vercel), strip ports, reject forged junk, and fall back to the peer."""

    @staticmethod
    def _make_request(xff: str = None, peer: str = "198.51.100.10"):
        from starlette.requests import Request

        headers = []
        if xff is not None:
            headers.append((b"x-forwarded-for", xff.encode()))
        scope = {"type": "http", "headers": headers, "client": (peer, 12345)}
        return Request(scope)

    def test_uses_first_xff_entry(self):
        from app.security import get_client_ip

        req = self._make_request(xff="203.0.113.9, 10.0.0.1, 172.16.0.1")
        assert get_client_ip(req) == "203.0.113.9"

    def test_strips_ipv4_port(self):
        from app.security import get_client_ip

        req = self._make_request(xff="203.0.113.9:5678")
        assert get_client_ip(req) == "203.0.113.9"

    def test_ipv6_address_preserved(self):
        from app.security import get_client_ip

        req = self._make_request(xff="2001:db8::1")
        assert get_client_ip(req) == "2001:db8::1"

    def test_malformed_xff_falls_back_to_peer(self):
        from app.security import get_client_ip

        req = self._make_request(xff="not-an-ip<script>")
        assert get_client_ip(req) == "198.51.100.10"

    def test_missing_xff_falls_back_to_peer(self):
        from app.security import get_client_ip

        req = self._make_request(xff=None)
        assert get_client_ip(req) == "198.51.100.10"

    def test_no_client_falls_back_to_unknown(self):
        from app.security import get_client_ip

        req = self._make_request(xff=None, peer=None)
        assert get_client_ip(req) == "unknown"


class TestFailClosedConfig:
    """get_settings() must fail closed on insecure JWT secrets."""

    GOOD_SECRET = "this-is-a-32-char-secret-for-testing-only!!"

    @staticmethod
    def _fresh_settings(monkeypatch, jwt_secret: str):
        monkeypatch.setenv("JWT_SECRET", jwt_secret)
        monkeypatch.delenv("JWT_VERIFY_SECRET", raising=False)
        monkeypatch.delenv("JWT_RESET_SECRET", raising=False)
        from app.config import get_settings
        get_settings.cache_clear()
        return get_settings

    def test_default_changeme_secret_rejected(self, monkeypatch):
        get_settings = self._fresh_settings(monkeypatch, jwt_secret="changeme")
        with pytest.raises(ValueError, match="JWT_SECRET"):
            get_settings()
        get_settings.cache_clear()

    def test_short_secret_rejected(self, monkeypatch):
        get_settings = self._fresh_settings(monkeypatch, jwt_secret="tooshort")
        with pytest.raises(ValueError, match="at least 32"):
            get_settings()
        get_settings.cache_clear()

    def test_valid_secret_accepted(self, monkeypatch):
        get_settings = self._fresh_settings(monkeypatch, jwt_secret=self.GOOD_SECRET)
        settings = get_settings()
        assert settings.jwt_secret == self.GOOD_SECRET
        get_settings.cache_clear()

    def test_verify_and_reset_secrets_auto_derived(self, monkeypatch):
        """When not set, verify/reset secrets are derived from the main JWT secret (unique, deterministic)."""
        import hashlib

        secret = "x" * 40
        get_settings = self._fresh_settings(monkeypatch, jwt_secret=secret)
        settings = get_settings()

        expected_verify = hashlib.sha256((secret + "__verify__").encode()).hexdigest()
        expected_reset = hashlib.sha256((secret + "__reset__").encode()).hexdigest()
        assert settings.jwt_verify_secret == expected_verify
        assert settings.jwt_reset_secret == expected_reset
        assert settings.jwt_verify_secret != secret
        assert settings.jwt_reset_secret != secret
        assert settings.jwt_verify_secret != settings.jwt_reset_secret
        get_settings.cache_clear()

    def test_explicit_verify_reset_secrets_respected(self, monkeypatch):
        """Explicitly configured verify/reset secrets are used as-is (not overwritten)."""
        monkeypatch.setenv("JWT_SECRET", self.GOOD_SECRET)
        monkeypatch.setenv("JWT_VERIFY_SECRET", "custom-verify-secret")
        monkeypatch.setenv("JWT_RESET_SECRET", "custom-reset-secret")
        from app.config import get_settings
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.jwt_verify_secret == "custom-verify-secret"
        assert settings.jwt_reset_secret == "custom-reset-secret"
        get_settings.cache_clear()


class TestProductionAppUrlGuard:
    """In production, get_settings() must never let APP_URL point at a dead domain
    (torrentcdn.com) or a plain http:// URL — those silently break email
    verification/reset links, billing redirects, and embed URLs."""

    GOOD_SECRET = "this-is-a-32-char-secret-for-testing-only!!"

    @staticmethod
    def _fresh_prod_settings(monkeypatch, app_url: str):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("JWT_SECRET", TestProductionAppUrlGuard.GOOD_SECRET)
        monkeypatch.setenv("APP_URL", app_url)
        monkeypatch.delenv("JWT_VERIFY_SECRET", raising=False)
        monkeypatch.delenv("JWT_RESET_SECRET", raising=False)
        monkeypatch.delenv("EMAIL_VERIFICATION_REQUIRED", raising=False)
        from app.config import get_settings
        get_settings.cache_clear()
        return get_settings

    def test_dead_apex_domain_overridden(self, monkeypatch):
        """https://torrentcdn.com (dead DNS) → corrected to the canonical frontend."""
        get_settings = self._fresh_prod_settings(monkeypatch, "https://torrentcdn.com")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_dead_www_domain_overridden(self, monkeypatch):
        """https://www.torrentcdn.com (dead DNS) → corrected to the canonical frontend."""
        get_settings = self._fresh_prod_settings(monkeypatch, "https://www.torrentcdn.com")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_http_scheme_upgraded_to_https(self, monkeypatch):
        """http:// APP_URL → upgraded to https:// (HSTS would block http links)."""
        get_settings = self._fresh_prod_settings(monkeypatch, "http://www.torrentcdn.store")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_http_dead_domain_combined_ordering(self, monkeypatch):
        """http:// + dead domain: the dead-domain override must win over the http upgrade."""
        get_settings = self._fresh_prod_settings(monkeypatch, "http://torrentcdn.com")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_trailing_slash_dead_domain(self, monkeypatch):
        """A trailing slash must not defeat the dead-domain override (hostname parsed clean)."""
        get_settings = self._fresh_prod_settings(monkeypatch, "https://torrentcdn.com/")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_port_variant_dead_domain(self, monkeypatch):
        """A port must not defeat the dead-domain override (hostname excludes the port)."""
        get_settings = self._fresh_prod_settings(monkeypatch, "https://torrentcdn.com:443")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_combined_www_port_slash_dead_domain(self, monkeypatch):
        """www + port + trailing slash on the dead domain → still canonicalized."""
        get_settings = self._fresh_prod_settings(monkeypatch, "https://www.torrentcdn.com:8443/")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_uppercase_host_dead_domain(self, monkeypatch):
        """Uppercase hostname is lowercased by urlparse, so the dead-domain override still fires."""
        get_settings = self._fresh_prod_settings(monkeypatch, "HTTPS://TORRENTCDN.COM")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_trailing_slash_canonical_domain(self, monkeypatch):
        """A correct domain with a trailing slash is left alone — the canonical form wins."""
        get_settings = self._fresh_prod_settings(monkeypatch, "https://www.torrentcdn.store/")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store/"
        get_settings.cache_clear()

    def test_localhost_flagged_but_not_guessed(self, monkeypatch, caplog):
        """localhost in production logs an error and is NOT auto-rewritten (can't guess the domain)."""
        import logging

        get_settings = self._fresh_prod_settings(monkeypatch, "http://localhost:3000")
        with caplog.at_level(logging.ERROR, logger="app.config"):
            settings = get_settings()
        assert settings.app_url == "http://localhost:3000"
        assert any("APP_URL points at localhost" in r.message for r in caplog.records)
        get_settings.cache_clear()

    def test_canonical_domain_untouched(self, monkeypatch):
        """A correct https://www.torrentcdn.store APP_URL passes through unchanged."""
        get_settings = self._fresh_prod_settings(monkeypatch, "https://www.torrentcdn.store")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_uppercase_http_scheme_upgraded(self, monkeypatch):
        """HTTP:// (uppercase) is still treated as insecure and upgraded to https://."""
        get_settings = self._fresh_prod_settings(monkeypatch, "HTTP://www.torrentcdn.store")
        settings = get_settings()
        assert settings.app_url == "https://www.torrentcdn.store"
        get_settings.cache_clear()

    def test_scheme_less_app_url_flagged(self, monkeypatch, caplog):
        """APP_URL without a scheme/hostname logs an error (fail loudly, don't guess)."""
        import logging

        get_settings = self._fresh_prod_settings(monkeypatch, "torrentcdn.store")
        with caplog.at_level(logging.ERROR, logger="app.config"):
            settings = get_settings()
        assert settings.app_url == "torrentcdn.store"
        assert any("no usable scheme/hostname" in r.message for r in caplog.records)
        get_settings.cache_clear()

    def test_scheme_without_host_flagged(self, monkeypatch, caplog):
        """A bare scheme like 'http://' (no hostname) fails loudly instead of a silent 'https://' rewrite."""
        import logging

        get_settings = self._fresh_prod_settings(monkeypatch, "http://")
        with caplog.at_level(logging.ERROR, logger="app.config"):
            settings = get_settings()
        assert settings.app_url == "http://"
        assert any("no usable scheme/hostname" in r.message for r in caplog.records)
        get_settings.cache_clear()

    def test_verification_required_defaults_on_in_production(self, monkeypatch):
        """Production with EMAIL_VERIFICATION_REQUIRED unset → verification gate on."""
        get_settings = self._fresh_prod_settings(monkeypatch, "https://www.torrentcdn.store")
        settings = get_settings()
        assert settings.email_verification_required is True
        get_settings.cache_clear()


class TestClockSkewGuard:
    """check_clock_skew() must fail loudly (ERROR) when the app clock drifts
    from an NTP reference beyond the threshold, stay quiet within it, and never
    raise when the reference is unreachable (fail-open)."""

    @staticmethod
    def _prod_settings():
        from app.config import Settings
        return Settings(app_env="production", clock_skew_max_minutes=5)

    def test_skipped_outside_production(self, caplog):
        """In development the guard is a no-op — no fetch, no logs."""
        import logging
        from app.config import Settings, check_clock_skew

        s = Settings(app_env="development")
        with caplog.at_level(logging.WARNING, logger="app.config"):
            check_clock_skew(s)
        assert not any("clock skew" in r.message.lower() for r in caplog.records)

    def test_logs_error_when_clock_ahead(self, caplog, monkeypatch):
        """Clock 2h AHEAD of real time → ERROR."""
        import logging
        from app.config import check_clock_skew

        monkeypatch.setattr("app.config._fetch_reference_epoch", lambda: time.time() + 7200)
        with caplog.at_level(logging.ERROR, logger="app.config"):
            check_clock_skew(self._prod_settings())
        assert any("CLOCK SKEW" in r.message for r in caplog.records)

    def test_logs_error_when_clock_behind(self, caplog, monkeypatch):
        """Clock 2h BEHIND real time → ERROR (direction doesn't matter)."""
        import logging
        from app.config import check_clock_skew

        monkeypatch.setattr("app.config._fetch_reference_epoch", lambda: time.time() - 7200)
        with caplog.at_level(logging.ERROR, logger="app.config"):
            check_clock_skew(self._prod_settings())
        assert any("CLOCK SKEW" in r.message for r in caplog.records)

    def test_no_error_within_threshold(self, caplog, monkeypatch):
        """Skew under the threshold → no ERROR (info-level only)."""
        import logging
        from app.config import check_clock_skew

        monkeypatch.setattr("app.config._fetch_reference_epoch", lambda: time.time() + 30)
        with caplog.at_level(logging.ERROR, logger="app.config"):
            check_clock_skew(self._prod_settings())
        assert not any("CLOCK SKEW" in r.message for r in caplog.records)

    def test_fail_open_when_reference_unreachable(self, caplog, monkeypatch):
        """No NTP reference → warning, no raise, no ERROR."""
        import logging
        from app.config import check_clock_skew

        monkeypatch.setattr("app.config._fetch_reference_epoch", lambda: None)
        with caplog.at_level(logging.WARNING, logger="app.config"):
            check_clock_skew(self._prod_settings())  # must not raise
        assert any("no NTP reference reachable" in r.message for r in caplog.records)

    def test_fail_open_when_fetch_raises(self, caplog, monkeypatch):
        """A raising reference fetch → warning, no crash."""
        import logging
        from app.config import check_clock_skew

        def boom():
            raise OSError("network down")

        monkeypatch.setattr("app.config._fetch_reference_epoch", boom)
        with caplog.at_level(logging.WARNING, logger="app.config"):
            check_clock_skew(self._prod_settings())  # must not raise
        assert any("skipped" in r.message for r in caplog.records)

    def test_reference_epoch_never_raises(self, monkeypatch):
        """_fetch_reference_epoch() returns None (never raises) when every source fails."""
        from app.config import _fetch_reference_epoch

        def _raise(*args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        assert _fetch_reference_epoch(timeout=0.001) is None

    def test_settings_default_threshold(self):
        """CLOCK_SKEW_MAX_MINUTES defaults to 5 minutes."""
        from app.config import Settings

        assert Settings().clock_skew_max_minutes == 5


class TestInMemoryRateLimitFallbacks:
    """When Redis is unavailable, in-memory stores must still enforce rate limits."""

    LOGIN_URL = "/auth/login"
    FORGOT_URL = "/auth/forgot-password"

    def _disable_redis(self, monkeypatch, route_module):
        """Force a route module's get_redis to return None (Redis down)."""
        monkeypatch.setattr(route_module, "get_redis", AsyncMock(return_value=None))

    def test_login_lockout_in_memory_fallback(self, client, mock_conn, monkeypatch):
        """Login lockout still works with the in-memory store when Redis is down."""
        import app.routes.auth as auth_route
        self._disable_redis(monkeypatch, auth_route)

        saved = dict(auth_route._login_attempts)
        auth_route._login_attempts.clear()
        try:
            for _ in range(5):
                resp = client.post(self.LOGIN_URL, json={"email": "user@example.com", "password": "wrongPass1"})
                assert resp.status_code == 401
            resp = client.post(self.LOGIN_URL, json={"email": "user@example.com", "password": "wrongPass1"})
            assert resp.status_code == 429
            assert "Too many login attempts" in resp.json()["detail"]
        finally:
            auth_route._login_attempts.clear()
            auth_route._login_attempts.update(saved)

    def test_login_in_memory_keyed_by_ip_and_email(self, client, mock_conn, monkeypatch):
        """In-memory fallback keeps the IP+email scoping: attacker can't block victim."""
        import app.routes.auth as auth_route
        self._disable_redis(monkeypatch, auth_route)

        saved = dict(auth_route._login_attempts)
        auth_route._login_attempts.clear()
        try:
            for _ in range(5):
                client.post(self.LOGIN_URL, json={"email": "attacker@example.com", "password": "wrongPass1"})
            # Victim's account (different email, same IP) is NOT locked out
            _mock_bcrypt_user(mock_conn, email="user@example.com")
            resp = client.post(self.LOGIN_URL, json={"email": "user@example.com", "password": "securePass123"})
            assert resp.status_code == 200
        finally:
            auth_route._login_attempts.clear()
            auth_route._login_attempts.update(saved)

    def test_forgot_password_in_memory_fallback(self, client, monkeypatch):
        """forgot-password is limited to 1 request/60s per IP even without Redis."""
        import app.routes.auth as auth_route
        self._disable_redis(monkeypatch, auth_route)

        saved = dict(auth_route._forgot_requests)
        auth_route._forgot_requests.clear()
        try:
            resp = client.post(self.FORGOT_URL, json={"email": "user@example.com"})
            assert resp.status_code == 200
            resp = client.post(self.FORGOT_URL, json={"email": "user@example.com"})
            assert resp.status_code == 429
            assert "Please wait" in resp.json()["detail"]
        finally:
            auth_route._forgot_requests.clear()
            auth_route._forgot_requests.update(saved)

    def test_forgot_password_in_memory_keyed_on_xff(self, client, monkeypatch):
        """In-memory forgot-password limit keys on the X-Forwarded-For client IP."""
        import app.routes.auth as auth_route
        self._disable_redis(monkeypatch, auth_route)

        saved = dict(auth_route._forgot_requests)
        auth_route._forgot_requests.clear()
        try:
            # Seed the store with a recent request from the XFF client IP
            auth_route._forgot_requests["203.0.113.9"] = time.time()
            resp = client.post(
                self.FORGOT_URL,
                json={"email": "user@example.com"},
                headers={"X-Forwarded-For": "203.0.113.9"},
            )
            assert resp.status_code == 429
            assert "Please wait" in resp.json()["detail"]
        finally:
            auth_route._forgot_requests.clear()
            auth_route._forgot_requests.update(saved)

    def test_public_file_rate_limit_in_memory_fallback(self, client, monkeypatch):
        """GET /files/public/{id} is rate limited (30/min) even without Redis."""
        import app.routes.files as files_route
        self._disable_redis(monkeypatch, files_route)

        saved = dict(files_route._public_file_requests)
        files_route._public_file_requests.clear()
        try:
            # Seed the in-memory store with 30 requests in the window for this IP
            files_route._public_file_requests["testclient"] = [time.time()] * 30
            resp = client.get("/files/public/11111111-1111-4111-8111-111111111111")
            assert resp.status_code == 429
            assert "Rate limit exceeded" in resp.json()["detail"]
        finally:
            files_route._public_file_requests.clear()
            files_route._public_file_requests.update(saved)

    def test_analytics_get_rate_limit_in_memory_fallback(self, client, auth_headers, monkeypatch):
        """GET /analytics/{file_id} is rate limited (30/min) even without Redis."""
        import app.routes.analytics as analytics_route
        self._disable_redis(monkeypatch, analytics_route)

        saved = dict(analytics_route._analytics_get_requests)
        analytics_route._analytics_get_requests.clear()
        try:
            analytics_route._analytics_get_requests["testclient"] = [time.time()] * 30
            resp = client.get("/analytics/11111111-1111-4111-8111-111111111111", headers=auth_headers)
            assert resp.status_code == 429
            assert "Rate limit exceeded" in resp.json()["detail"]
        finally:
            analytics_route._analytics_get_requests.clear()
            analytics_route._analytics_get_requests.update(saved)

    def test_upload_rate_limit_in_memory_fallback(self, client, auth_headers, mock_conn, monkeypatch):
        """POST /upload is limited (10/hour per IP) even without Redis."""
        import app.routes.upload as upload_route
        self._disable_redis(monkeypatch, upload_route)

        # The limiter fires after validation/usage checks — provide a user row
        # so the request reaches the storage-bound limiter.
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,  # no usage record
        ]

        saved = dict(upload_route._upload_requests)
        upload_route._upload_requests.clear()
        try:
            upload_route._upload_requests["testclient"] = [time.time()] * 10
            resp = client.post(
                "/upload",
                headers=auth_headers,
                files={"file": ("test.mp4", BytesIO(b"test"), "video/mp4")},
            )
            assert resp.status_code == 429
            assert "Upload limit reached" in resp.json()["detail"]
        finally:
            upload_route._upload_requests.clear()
            upload_route._upload_requests.update(saved)

    def test_global_middleware_in_memory_fallback(self, client, monkeypatch):
        """The global 5 req/10s middleware limiter still works when Redis is down.

        Note: the FastAPI app is a module singleton, so mw._memory_store persists for
        the whole session. Only this test forces app.database.get_redis → None, so
        save/clear/restore around it keeps other tests safe. Any future test that
        patches get_redis to None must do the same.
        """
        import app.database
        monkeypatch.setattr(app.database, "get_redis", AsyncMock(return_value=None))

        from app.main import RateLimitMiddleware
        mw = self._find_rate_limit_middleware(client.app)
        assert mw is not None, "RateLimitMiddleware not found in middleware stack"

        saved = dict(mw._memory_store)
        mw._memory_store.clear()
        try:
            responses = []
            for _ in range(6):
                # /files is not rate-limit-exempt (unlike /health); auth returns 401,
                # but the middleware still counts the request before the route runs.
                responses.append(client.get("/files"))
            assert [r.status_code for r in responses[:5]] == [401] * 5
            assert responses[5].status_code == 429
            assert "Rate limit exceeded" in responses[5].json()["detail"]
        finally:
            mw._memory_store.clear()
            mw._memory_store.update(saved)

    @staticmethod
    def _find_rate_limit_middleware(app):
        """Walk the ASGI middleware chain to find the RateLimitMiddleware instance."""
        from app.main import RateLimitMiddleware
        stack = getattr(app, "middleware_stack", None)
        depth = 0
        while stack is not None and depth < 20:
            if isinstance(stack, RateLimitMiddleware):
                return stack
            stack = getattr(stack, "app", None)
            depth += 1
        return None
