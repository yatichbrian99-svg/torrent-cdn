"""
Unit tests for auth routes: register, login, and get current user.
"""
from unittest.mock import AsyncMock
from uuid import UUID

import pytest


class TestRegister:
    """Tests for POST /auth/register."""

    REGISTER_URL = "/auth/register"

    def test_register_success(self, client, mock_conn):
        """Should register a new user and return a JWT token."""
        # First fetchrow (SELECT existing user) returns None, second (INSERT) returns id
        mock_conn.fetchrow.side_effect = [
            None,  # no existing user
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
        mock_conn.fetchrow.return_value = {"id": "11111111-1111-4111-8111-111111111111"}

        resp = client.post(self.REGISTER_URL, json={
            "email": "existing@example.com",
            "password": "securePass123",
            "terms_accepted": True,
        })

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Email already registered"

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
        mock_conn.fetchrow.side_effect = [
            None,
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

    def test_login_rate_limiting(self, client, mock_conn, mock_redis):
        """Should rate-limit after 5 failed login attempts."""
        mock_redis.get.return_value = "5"

        resp = client.post(self.LOGIN_URL, json={
            "email": "hacker@example.com",
            "password": "bruteForce123",
        })
        assert resp.status_code == 429
        assert "Rate limit" in resp.json()["detail"]  # Matches both global and login-specific messages

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

        # Key-aware mock: only the reset_token key is populated. The global
        # RateLimitMiddleware reads the same mock for ratelimit:{ip}:global and
        # would raise ValueError on int("11111111-…") if we returned the UUID
        # string for every key.
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

        # Only the reset_token key looks consumed; the global RateLimitMiddleware
        # reads this same mock for ratelimit:{ip}:global and must see None.
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
        # The UPDATE must set verified = TRUE alongside the new password hash.
        sql = mock_conn.execute.call_args.args[0].lower()
        assert "verified" in sql
        assert "set password_hash = $1, verified = true" in sql

    def test_reset_password_redis_down_at_click_uses_jwt_only(self, client, mock_conn, monkeypatch):
        """
        Asymmetry control: if Redis is down at CLICK time, the key check is
        skipped entirely and the same valid token works — JWT expiry is the
        only gate. The missing-key fallback above behaves identically.
        """
        token = self._valid_reset_token()

        import app.routes.auth
        monkeypatch.setattr(app.routes.auth, "get_redis", AsyncMock(return_value=None))
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post(self.RESET_URL, json={"token": token, "password": "NewPass456!"})

        assert resp.status_code == 200
        assert "reset successfully" in resp.json()["detail"]
