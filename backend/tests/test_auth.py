"""
Unit tests for auth routes: register, login, and get current user.
"""
from unittest.mock import AsyncMock
from uuid import UUID


class TestRegister:
    """Tests for POST /auth/register."""

    REGISTER_URL = "/auth/register"

    def test_register_success(self, client, mock_conn):
        """Should register a new user and return a JWT token."""
        # H1 FIX: there is no pre-check SELECT anymore — the INSERT (with its
        # unique constraint on users.email) is the first and only fetchrow.
        mock_conn.fetchrow.side_effect = [
            {"id": "11111111-1111-4111-8111-111111111111"},  # insert result
        ]

        resp = client.post(self.REGISTER_URL, json={
            "email": "newuser@example.com",
            "password": "securePass123",
            "terms_accepted": True,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_register_duplicate_email(self, client, mock_conn):
        """Should reject duplicate email registration."""
        # H1 FIX: duplicates are caught by the INSERT's unique constraint (no
        # pre-check SELECT), and the error is deliberately GENERIC to prevent
        # email enumeration — an attacker can't distinguish "already registered"
        # from "registration failed".
        from asyncpg.exceptions import UniqueViolationError
        mock_conn.fetchrow.side_effect = UniqueViolationError(
            'duplicate key value violates unique constraint "users_email_key"'
        )

        resp = client.post(self.REGISTER_URL, json={
            "email": "existing@example.com",
            "password": "securePass123",
            "terms_accepted": True,
        })

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Registration failed. Please try again."

    def test_register_invalid_email(self, client):
        """Should reject invalid email format."""
        resp = client.post(self.REGISTER_URL, json={
            "email": "not-an-email",
            "password": "securePass123",
        })
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid email address"

    def test_register_short_password(self, client):
        """Should reject passwords shorter than 8 characters."""
        resp = client.post(self.REGISTER_URL, json={
            "email": "user@example.com",
            "password": "abc1234",
        })
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Password must be at least 8 characters"

    def test_register_no_letter_password(self, client):
        """Should reject passwords without any letters."""
        resp = client.post(self.REGISTER_URL, json={
            "email": "user@example.com",
            "password": "12345678",
        })
        assert resp.status_code == 400
        assert "letter" in resp.json()["detail"]

    def test_register_no_number_password(self, client):
        """Should reject passwords without any numbers."""
        resp = client.post(self.REGISTER_URL, json={
            "email": "user@example.com",
            "password": "abcdefgh",
        })
        assert resp.status_code == 400
        assert "number" in resp.json()["detail"]

    def test_register_exactly_8_chars_password(self, client, mock_conn):
        """Should accept password with exactly 8 characters (boundary)."""
        # H1 FIX: no pre-check SELECT — the INSERT is the first fetchrow.
        mock_conn.fetchrow.side_effect = [
            {"id": "11111111-1111-4111-8111-111111111111"},
        ]

        resp = client.post(self.REGISTER_URL, json={
            "email": "boundary@example.com",
            "password": "abcd1234",
            "terms_accepted": True,
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_register_empty_email(self, client):
        """Should reject empty email."""
        resp = client.post(self.REGISTER_URL, json={
            "email": "",
            "password": "securePass123",
        })
        assert resp.status_code == 400
        assert "Invalid email" in resp.json()["detail"]

    def test_register_terms_not_accepted(self, client):
        """Should reject registration when terms are not accepted."""
        resp = client.post(self.REGISTER_URL, json={
            "email": "user@example.com",
            "password": "securePass123",
            "terms_accepted": False,
        })
        assert resp.status_code == 400
        assert "Terms of Service" in resp.json()["detail"]

    def test_register_terms_default_false(self, client):
        """Should reject registration when terms_accepted is omitted (defaults to False)."""
        resp = client.post(self.REGISTER_URL, json={
            "email": "user@example.com",
            "password": "securePass123",
        })
        assert resp.status_code == 400
        assert "Terms of Service" in resp.json()["detail"]

    def test_register_missing_fields(self, client):
        """Should reject request without required fields."""
        resp = client.post(self.REGISTER_URL, json={})
        assert resp.status_code == 422


class TestLogin:
    """Tests for POST /auth/login."""

    LOGIN_URL = "/auth/login"

    def _mock_bcrypt_user(self, mock_conn, password="securePass123"):
        """Set up mock_conn with a bcrypt-hashed password that will pass verification."""
        from app.auth import hash_password
        hashed = hash_password(password)
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "user@example.com",
            "password_hash": hashed,
            "verified": True,
        }

    def test_login_success(self, client, mock_conn):
        """Should login with valid credentials and return a JWT token."""
        self._mock_bcrypt_user(mock_conn)

        resp = client.post(self.LOGIN_URL, json={
            "email": "user@example.com",
            "password": "securePass123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_wrong_password(self, client, mock_conn, mock_redis):
        """Should reject login with wrong password."""
        self._mock_bcrypt_user(mock_conn)

        resp = client.post(self.LOGIN_URL, json={
            "email": "user@example.com",
            "password": "wrongPassword1",
        })
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password"

    def test_login_nonexistent_user(self, client, mock_conn, mock_redis):
        """Should reject login for non-existent user."""
        mock_conn.fetchrow.return_value = None

        resp = client.post(self.LOGIN_URL, json={
            "email": "nonexistent@example.com",
            "password": "securePass123",
        })
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password"

    def test_global_budget_does_not_block_login(self, client, mock_conn, mock_redis):
        """An exhausted GLOBAL per-IP budget must NOT block /auth/login.

        /auth/* is exempt from the global burst limiter — every auth endpoint
        carries its own dedicated control, and the login lockout (5 failures
        for IP+email, covered in test_security.py::TestLoginLockout) is the
        gate that matters. The global budget used to 429 legitimate logins on
        busy shared/NAT IPs with a "Try again in 10s" that never cleared while
        other traffic on the shared key kept the bucket full.

        Seed the global key AT the limit and prove login sails past the
        middleware to the route (wrong password → 401, not a middleware 429)."""
        from app.config import get_settings

        def fake_get(key):
            if key == "ratelimit:testclient:global":
                return str(get_settings().rate_limit_global)  # global budget exhausted
            return None  # login lockout not tripped

        mock_redis.get.side_effect = fake_get
        mock_conn.fetchrow.return_value = None  # no such user

        resp = client.post(self.LOGIN_URL, json={
            "email": "hacker@example.com",
            "password": "bruteForce123",
        })
        # Reached the route (invalid credentials) — NOT a middleware 429.
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password"

    def test_login_unverified_blocked(self, client, mock_conn, monkeypatch):
        """Should block login for unverified users when verification is required."""
        from app.auth import hash_password
        hashed = hash_password("securePass123")
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "unverified@example.com",
            "password_hash": hashed,
            "verified": False,
        }

        # Enable email verification for this test by patching the route module's local reference
        import app.routes.auth
        monkeypatch.setattr(app.routes.auth, "get_settings", lambda: type("Settings", (), {
            "email_verification_required": True,
        })())

        resp = client.post(self.LOGIN_URL, json={
            "email": "unverified@example.com",
            "password": "securePass123",
        })
        assert resp.status_code == 403
        assert "Email not verified" in resp.json()["detail"]

    def test_login_missing_fields(self, client):
        """Should reject login without required fields."""
        resp = client.post(self.LOGIN_URL, json={})
        assert resp.status_code == 422


class TestLoginAuditLog:
    """Login success/failure must be logged (auth-event audit trail).

    Failed logins are logged with email + client IP (never the password) so
    brute-force / credential-stuffing campaigns are visible even before the
    lockout counter trips; successes log the user id only.
    """

    LOGIN_URL = "/auth/login"

    def test_failed_login_logged_with_email_and_ip(self, client, mock_conn, caplog):
        """Wrong credentials → a WARNING naming the email and client IP."""
        mock_conn.fetchrow.return_value = None  # unknown user (enum-timing path)

        import logging
        with caplog.at_level(logging.WARNING, logger="app.routes.auth"):
            resp = client.post(self.LOGIN_URL, json={
                "email": "attacker@example.com",
                "password": "wrongPass1",
            })

        assert resp.status_code == 401
        failed = [r for r in caplog.records if r.message.startswith("Login failed for")]
        assert len(failed) == 1
        assert "attacker@example.com" in failed[0].message
        assert "testclient" in failed[0].message  # TestClient peer IP
        assert "wrongPass1" not in failed[0].message  # never log the password

    def test_successful_login_logged_with_user_id(self, client, mock_conn, caplog):
        """Valid credentials → an INFO record naming the user id."""
        from app.auth import hash_password
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "user@example.com",
            "password_hash": hash_password("securePass123"),
            "verified": True,
        }

        import logging
        with caplog.at_level(logging.INFO, logger="app.routes.auth"):
            resp = client.post(self.LOGIN_URL, json={
                "email": "user@example.com",
                "password": "securePass123",
            })

        assert resp.status_code == 200
        ok = [r for r in caplog.records if r.message.startswith("Login succeeded for user")]
        assert len(ok) == 1
        assert "11111111-1111-4111-8111-111111111111" in ok[0].message


class TestGetMe:
    """Tests for GET /auth/me."""

    ME_URL = "/auth/me"

    def test_get_me_success(self, client, auth_headers, mock_conn):
        """Should return current user info with valid token."""
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "user@example.com",
            "plan": "free",
            "created_at": "2026-01-01T00:00:00Z",
        }

        resp = client.get(self.ME_URL, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "user@example.com"
        assert data["plan"] == "free"

    def test_get_me_no_token(self, client):
        """Should reject request without auth token (401 Unauthorized)."""
        resp = client.get(self.ME_URL)
        assert resp.status_code == 401  # HTTPBearer returns 401 when missing

    def test_get_me_invalid_token(self, client):
        """Should reject request with invalid auth token."""
        resp = client.get(self.ME_URL, headers={"Authorization": "Bearer invalidtoken"})
        assert resp.status_code == 401
        assert "Invalid or expired token" in resp.json()["detail"]

    def test_get_me_user_not_found(self, client, auth_headers, mock_conn):
        """Should return 404 if user doesn't exist in DB."""
        mock_conn.fetchrow.return_value = None

        resp = client.get(self.ME_URL, headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "User not found"


class TestLogoutTokenRevocation:
    """Regression: POST /auth/logout must actually invalidate the session token.

    Logout writes ``token_block:{jti}`` to Redis. The check in
    ``get_current_user_id`` used to raise its 401 *inside* a ``try`` whose bare
    ``except Exception: pass`` caught that same HTTPException (an Exception
    subclass), so the blocklist was a silent no-op: the key was written, and
    every request still authenticated. These tests pin both the rejection and
    the intended fail-open behaviour on a Redis outage.
    """

    ME_URL = "/auth/me"

    @staticmethod
    def _jti(headers):
        from app.auth import decode_access_token
        token = headers["Authorization"].removeprefix("Bearer ")
        return decode_access_token(token)["jti"]

    def test_blocklisted_jti_is_rejected(self, client, auth_headers, mock_conn, monkeypatch):
        """A token whose jti is on the blocklist must 401 (returned 200 pre-fix)."""
        jti = self._jti(auth_headers)
        redis = AsyncMock()
        redis.get = AsyncMock(return_value="1")
        monkeypatch.setattr("app.auth.get_redis", AsyncMock(return_value=redis))
        # The user row exists — the blocklist must short-circuit before it is read.
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111")
        }

        resp = client.get(self.ME_URL, headers=auth_headers)
        assert resp.status_code == 401, (
            "a logged-out token still authenticated — the blocklist raise is being "
            "swallowed by its own except Exception"
        )
        assert "Session invalidated" in resp.json()["detail"]
        assert jti  # the token really carried the jti the blocklist keys on

    def test_non_blocklisted_jti_still_authenticates(self, client, auth_headers, mock_conn, monkeypatch):
        """An unblocked token must keep working — guards against over-blocking."""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        monkeypatch.setattr("app.auth.get_redis", AsyncMock(return_value=redis))
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "user@example.com",
            "plan": "free",
            "created_at": "2026-01-01T00:00:00Z",
        }

        resp = client.get(self.ME_URL, headers=auth_headers)
        assert resp.status_code == 200

    def test_redis_failure_fails_open(self, client, auth_headers, mock_conn, monkeypatch):
        """A Redis outage must not lock users out — fail open, expiry is the backstop.

        This is the behaviour the broad except existed to provide; the fix must
        keep it while letting the deliberate 401 escape.
        """
        async def _boom():
            raise ConnectionError("redis down")

        monkeypatch.setattr("app.auth.get_redis", _boom)
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "user@example.com",
            "plan": "free",
            "created_at": "2026-01-01T00:00:00Z",
        }

        resp = client.get(self.ME_URL, headers=auth_headers)
        assert resp.status_code == 200

    def test_logout_writes_the_blocklist_key(self, client, auth_headers, monkeypatch):
        """The other half of the contract: logout must write token_block:{jti}+TTL."""
        jti = self._jti(auth_headers)
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        monkeypatch.setattr("app.routes.auth.get_redis", AsyncMock(return_value=redis))
        monkeypatch.setattr("app.auth.get_redis", AsyncMock(return_value=redis))

        resp = client.post("/auth/logout", headers=auth_headers)
        assert resp.status_code == 200
        redis.set.assert_awaited_once()
        args, kwargs = redis.set.await_args
        assert args[0] == f"token_block:{jti}"
        assert kwargs.get("ex", 0) > 0


class TestAccessTokenClaimRequirements:
    """Regression (F-10): a session JWT must carry exp/sub/iat and a bounded lifetime.

    ``decode_access_token`` used to validate these claims only *when present*
    (``options={"require": ...}`` was absent). A token signed with the real
    secret and containing only ``sub`` therefore authenticated, never expired,
    and — because ``_token_revoked_by_password_change`` returns early on a
    missing ``iat`` — could not be revoked by a password reset or an MFA change
    either. That is a permanent, non-revocable credential if the signing key is
    ever leaked or rotated out.

    These tests pin the rejection of every malformed shape AND that normal
    app-issued tokens, including the longest legitimate one, still work.
    """

    ME_URL = "/auth/me"
    USER_ID = "11111111-1111-4111-8111-111111111111"
    DAY = 60 * 60 * 24

    @staticmethod
    def _mint(payload: dict) -> str:
        """Sign `payload` with the app's real secret (the leaked-key scenario)."""
        import jwt as pyjwt

        from app.config import get_settings
        return pyjwt.encode(
            payload, get_settings().jwt_secret, algorithm=get_settings().jwt_algorithm
        )

    def _user_row(self, mock_conn) -> None:
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.USER_ID),
            "email": "user@example.com",
            "plan": "free",
            "created_at": "2026-01-01T00:00:00Z",
        }

    def _get(self, client, mock_conn, payload: dict):
        self._user_row(mock_conn)
        token = self._mint(payload)
        return client.get(self.ME_URL, headers={"Authorization": f"Bearer {token}"})

    def test_bare_sub_token_is_rejected(self, client, mock_conn):
        """The exact F-10 PoC — sub only. Returned 200 before the fix."""
        resp = self._get(client, mock_conn, {"sub": self.USER_ID})
        assert resp.status_code == 401, (
            "a sub-only token still authenticated — it never expires and cannot "
            "be revoked by a password change"
        )

    def test_token_without_exp_is_rejected(self, client, mock_conn):
        """No exp means never expires — must fail closed."""
        import time
        resp = self._get(client, mock_conn,
                         {"sub": self.USER_ID, "iat": int(time.time())})
        assert resp.status_code == 401

    def test_token_without_iat_is_rejected(self, client, mock_conn):
        """No iat means the revocation check can never match it."""
        import time
        resp = self._get(client, mock_conn, {
            "sub": self.USER_ID,
            "exp": int(time.time()) + 3600,
        })
        assert resp.status_code == 401

    def test_token_without_sub_is_rejected(self, client, mock_conn):
        """No sub means no identity — must not fall through to a handler."""
        import time
        now = int(time.time())
        resp = self._get(client, mock_conn, {"exp": now + 3600, "iat": now})
        assert resp.status_code == 401

    def test_far_future_exp_is_rejected(self, client, mock_conn):
        """A ~100-year lifetime is malformed: the longest real one is 14 days."""
        import time
        now = int(time.time())
        resp = self._get(client, mock_conn, {
            "sub": self.USER_ID, "iat": now, "exp": now + 100 * 365 * self.DAY,
        })
        assert resp.status_code == 401, "an unbounded exp was accepted"

    def test_app_issued_token_still_authenticates(self, client, auth_headers, mock_conn):
        """Control: tightening the claims must not break real tokens."""
        self._user_row(mock_conn)
        resp = client.get(self.ME_URL, headers=auth_headers)
        assert resp.status_code == 200

    def test_remember_me_lifetime_is_accepted(self, client, mock_conn):
        """Control: the 14-day "remember me" lifetime is the longest legitimate
        one and must survive the new cap."""
        import time
        now = int(time.time())
        resp = self._get(client, mock_conn, {
            "sub": self.USER_ID, "iat": now, "exp": now + 14 * self.DAY,
        })
        assert resp.status_code == 200

    def test_a_longer_configured_lifetime_is_accepted(self, client, mock_conn, monkeypatch):
        """Backward compatibility: the cap follows JWT_EXPIRE_MINUTES.

        The ceiling has to cover whatever this deployment's own policy mints. An
        operator running 30-day sessions (a supported setting) would otherwise be
        locked out by tokens the app itself just issued — a rollout that breaks
        every login rather than one malformed credential.
        """
        import time

        from app import auth as auth_module
        monkeypatch.setattr(auth_module.settings, "jwt_expire_minutes", 30 * 24 * 60)
        now = int(time.time())
        resp = self._get(client, mock_conn, {
            "sub": self.USER_ID, "iat": now, "exp": now + 30 * self.DAY,
        })
        assert resp.status_code == 200

        # The ceiling moved with the policy, it did not disappear.
        resp = self._get(client, mock_conn, {
            "sub": self.USER_ID, "iat": now, "exp": now + 100 * 365 * self.DAY,
        })
        assert resp.status_code == 401

    def test_every_mint_path_emits_the_claims_the_decoder_requires(self):
        """No app-issued token may be rejected by the tightened decoder.

        F-10's blast radius is exactly "tokens this app mints that lack exp/sub/iat".
        Both mint functions are checked here, and each token's lifetime is checked
        against the decoder's own ceiling, so adding a mint path without `iat` — or
        raising the policy past the cap — fails here rather than in production as a
        login lockout.
        """
        import jwt as pyjwt

        from app.auth import (
            _max_token_lifetime_secs,
            create_access_token,
            decode_access_token,
        )
        from app.config import get_settings
        from app.mfa import create_mfa_token

        settings = get_settings()
        minted = {
            "create_access_token(remember_me=False)": (
                create_access_token({"sub": self.USER_ID}),
                True,
            ),
            "create_access_token(remember_me=True)": (
                create_access_token({"sub": self.USER_ID}, expires_minutes=14 * 24 * 60),
                True,
            ),
            # The decoder rejects mfa_pending on purpose (it is a login step, not a
            # session), so only the claim shape and lifetime are checked for it.
            "create_mfa_token": (create_mfa_token(self.USER_ID), False),
        }
        for name, (token, decodable) in minted.items():
            claims = pyjwt.decode(
                token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
            )
            assert {"exp", "sub", "iat"} <= claims.keys(), (
                f"{name} does not emit every claim decode_access_token requires"
            )
            lifetime = float(claims["exp"]) - float(claims["iat"])
            assert lifetime <= _max_token_lifetime_secs(), (
                f"{name} mints a {lifetime:,.0f}s token the decoder rejects "
                f"(cap {_max_token_lifetime_secs():,.0f}s)"
            )
            # The decoder is the authority: every session token must pass it.
            if decodable:
                assert decode_access_token(token) is not None, (
                    f"{name} is rejected by its own decoder"
                )


class TestDeleteMe:
    """Tests for DELETE /auth/me — self-account deletion."""

    DELETE_URL = "/auth/me"

    def test_delete_own_account_success(self, client, auth_headers, mock_conn, mock_s3_client):
        """Should delete the user's own account and return success."""
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "email": "user@example.com", "is_admin": False},
        ]
        mock_conn.fetch.return_value = []
        mock_conn.execute.return_value = "DELETE 1"

        resp = client.delete(self.DELETE_URL, headers=auth_headers)
        assert resp.status_code == 200
        assert "deleted successfully" in resp.json()["detail"]

    def test_delete_own_account_no_auth(self, client):
        """Should return 401 without auth."""
        resp = client.delete(self.DELETE_URL)
        assert resp.status_code == 401

    def test_delete_own_account_admin_blocked(self, client, auth_headers, mock_conn):
        """Should block self-deletion of admin accounts."""
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "admin@example.com",
            "is_admin": True,
        }

        resp = client.delete(self.DELETE_URL, headers=auth_headers)
        assert resp.status_code == 403
        assert "cannot be deleted" in resp.json()["detail"]

    def test_delete_own_account_not_found(self, client, auth_headers, mock_conn):
        """Should return 404 if user doesn't exist."""
        mock_conn.fetchrow.return_value = None

        resp = client.delete(self.DELETE_URL, headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "User not found"

    def test_delete_own_account_with_files(self, client, auth_headers, mock_conn, mock_s3_client):
        """Should clean up S3 files when deleting own account."""
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "email": "user@example.com", "is_admin": False},
        ]
        mock_conn.fetch.return_value = [
            {"id": UUID("33333333-3333-4333-8333-333333333333"), "minio_url": "http://minio:9000/bucket/user123/file456/video.mp4"},
        ]
        mock_conn.execute.return_value = "DELETE 1"

        resp = client.delete(self.DELETE_URL, headers=auth_headers)
        assert resp.status_code == 200
        mock_s3_client.delete_object.assert_called_once()


class TestResetPassword:
    """
    Tests for POST /auth/reset-password.

    Covers the Redis availability asymmetry: the one-time revocation key
    (`reset_token:{token}`) is only stored at SEND time when Redis is up. If
    Redis was down when the reset email was sent, the key is legitimately
    absent — a valid, unexpired JWT must still reset the password (fall back
    to JWT-expiry-only validation), matching the Redis-down-at-click path.
    When the key IS present it is consumed into a "CONSUMED" tombstone, so a
    replayed link is still rejected for the rest of the JWT window.
    """

    RESET_URL = "/auth/reset-password"
    USER_ID = UUID("11111111-1111-4111-8111-111111111111")

    def _valid_reset_token(self):
        """Mint a real, unexpired reset JWT for the test user (same secret as the route)."""
        from app.email_service import create_password_reset_token
        return create_password_reset_token(self.USER_ID)

    def test_reset_password_success_with_redis_key(self, client, mock_conn, mock_redis):
        """Should reset the password when the token AND its one-time Redis key exist."""
        token = self._valid_reset_token()

        # Key-aware mock: only the reset_token key is populated. /auth/* is
        # exempt from the global RateLimitMiddleware, so it never reads this
        # mock — but the key-aware shape keeps the route's own reads (and any
        # future middleware) from seeing the UUID string for unrelated keys.
        async def _fake_get(key):
            return str(self.USER_ID) if str(key).startswith("reset_token:") else None
        mock_redis.get.side_effect = _fake_get
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post(self.RESET_URL, json={"token": token, "password": "NewPass456!"})

        assert resp.status_code == 200
        assert "reset successfully" in resp.json()["detail"]
        # Consumed into a tombstone (not deleted) so replays stay blocked.
        # The route also records a pw_change marker (session revocation) —
        # assert the CONSUMED tombstone specifically.
        consumed_calls = [c for c in mock_redis.set.call_args_list if c.args[1] == "CONSUMED"]
        assert len(consumed_calls) == 1
        pw_calls = [c for c in mock_redis.set.call_args_list if "pw_change:" in str(c.args[0])]
        assert len(pw_calls) == 1

    def test_reset_password_valid_token_succeeds_when_redis_key_missing(self, client, mock_conn, mock_redis):
        """
        Edge case: Redis was DOWN when the reset email was sent, so the one-time
        key was never stored. When Redis is back up at click time, a VALID,
        unexpired token must STILL reset the password — the route falls back to
        JWT-expiry-only validation instead of rejecting it with 400.
        """
        token = self._valid_reset_token()

        # Prove the token itself is valid — success must come from the JWT
        # (not from a stored key, which is absent).
        from app.email_service import decode_password_reset_token
        assert decode_password_reset_token(token) == str(self.USER_ID)

        mock_redis.get.return_value = None  # key never stored (send-time outage)
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post(self.RESET_URL, json={"token": token, "password": "NewPass456!"})

        assert resp.status_code == 200
        assert "reset successfully" in resp.json()["detail"]
        mock_redis.delete.assert_not_called()  # no key to consume
        # No tombstone created for an absent key — only the pw_change marker
        # (session revocation) may be written.
        consumed_calls = [c for c in mock_redis.set.call_args_list if c.args[1] == "CONSUMED"]
        assert len(consumed_calls) == 0

    def test_reset_password_consumed_token_replay_rejected(self, client, mock_redis):
        """
        A token whose one-time key was already consumed (tombstoned as
        "CONSUMED") is rejected with 400 even though its JWT is still valid —
        one-time use survives Redis restarts for the rest of the JWT window.
        """
        token = self._valid_reset_token()

        from app.email_service import decode_password_reset_token
        assert decode_password_reset_token(token) == str(self.USER_ID)

        # Only the reset_token key looks consumed. (/auth/* is exempt from the
        # global RateLimitMiddleware, so the mock only serves the route's reads.)
        async def _fake_get(key):
            return "CONSUMED" if str(key).startswith("reset_token:") else None
        mock_redis.get.side_effect = _fake_get

        resp = client.post(self.RESET_URL, json={"token": token, "password": "NewPass456!"})

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid or expired reset token"
        mock_redis.delete.assert_not_called()
        mock_redis.set.assert_not_called()  # never re-consumes or re-tombstones

    def test_reset_password_marks_email_verified(self, client, mock_conn, mock_redis):
        """
        A successful reset proves email ownership (the token only reached that
        inbox), so the account is marked verified. This unblocks users whose
        verification email was lost/never delivered — production gates login on
        verified, so without this they'd be permanently locked out even after
        resetting.
        """
        token = self._valid_reset_token()

        async def _fake_get(key):
            return str(self.USER_ID) if str(key).startswith("reset_token:") else None
        mock_redis.get.side_effect = _fake_get
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post(self.RESET_URL, json={"token": token, "password": "NewPass456!"})

        assert resp.status_code == 200
        assert "reset successfully" in resp.json()["detail"]
        # The UPDATE must set verified = TRUE alongside the new password hash
        # (the security_stamp bump is a separate later call — search all calls).
        sqls = [str(c.args[0]).lower() for c in mock_conn.execute.call_args_list]
        assert any("set password_hash = $1, verified = true" in s for s in sqls)

    def test_reset_password_redis_down_at_click_uses_jwt_only(self, client, mock_conn, monkeypatch):
        """
        H1 FIX: When Redis is down at CLICK time, the DB fallback marker is
        checked. If the DB has 'PENDING' (set at send time when Redis was also
        down), the token is accepted and the marker is replaced with the hash.
        """
        token = self._valid_reset_token()

        import app.routes.auth
        monkeypatch.setattr(app.routes.auth, "get_redis", AsyncMock(return_value=None))
        # Mock fetchval to return 'PENDING' (the DB marker set at send time)
        mock_conn.fetchval.return_value = "PENDING"
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post(self.RESET_URL, json={"token": token, "password": "NewPass456!"})

        assert resp.status_code == 200
        assert "reset successfully" in resp.json()["detail"]


class TestUnstorableEmailChars:
    """A NUL byte is legal JSON/pydantic input but NOT legal Postgres text.

    The driver raises CharacterNotInRepertoireError ("invalid byte sequence for
    encoding UTF8: 0x00") at bind time, so before the guard every one of these
    requests answered 500 and logged a stack trace — three unauthenticated
    endpoints, usable as free error-log noise (found live by
    scripts/pentest_local.py). The guard must run BEFORE any query: the point of
    these tests is the 400, and that no DB round-trip happened.
    """

    NUL_EMAIL = "nul\x00l@t.st"

    def test_register_nul_in_email_is_400_not_500(self, client, mock_conn):
        resp = client.post("/auth/register", json={
            "email": self.NUL_EMAIL,
            "password": "securePass123",
            "terms_accepted": True,
        })
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid email address"
        assert mock_conn.fetchrow.call_count == 0

    def test_login_nul_in_email_is_400_not_500(self, client, mock_conn):
        resp = client.post("/auth/login", json={
            "email": self.NUL_EMAIL,
            "password": "securePass123",
        })
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid email address"
        assert mock_conn.fetchrow.call_count == 0

    def test_forgot_password_nul_in_email_is_400_not_500(self, client, mock_conn):
        resp = client.post("/auth/forgot-password", json={"email": self.NUL_EMAIL})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid email address"
        # The raw (pre-normalization) lookup is the FIRST query in this route, so
        # a guard placed after it would still 500 — assert it ran zero queries.
        assert mock_conn.fetchrow.call_count == 0

    def test_other_control_chars_are_rejected_too(self, client, mock_conn):
        """Tab/newline/DEL are control characters as well — same 400."""
        for bad in ("nul\tl@t.st", "nul\nl@t.st", "nul\x7fl@t.st"):
            resp = client.post("/auth/login", json={
                "email": bad, "password": "securePass123",
            })
            assert resp.status_code == 400, bad
        assert mock_conn.fetchrow.call_count == 0

    def test_normal_email_still_reaches_the_database(self, client, mock_conn):
        """The guard must not reject ordinary addresses (negative control)."""
        mock_conn.fetchrow.return_value = None  # no such user
        resp = client.post("/auth/login", json={
            "email": "normal@example.com", "password": "securePass123",
        })
        assert resp.status_code == 401
        # The lookup itself runs (raw, then the NFC fallback) — i.e. the guard
        # let this address through to the DB.
        assert mock_conn.fetchrow.call_count >= 1
