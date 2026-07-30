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
            "password": "1234567",
        })
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Password must be at least 8 characters"

    def test_register_exactly_8_chars_password(self, client, mock_conn):
        """Should accept password with exactly 8 characters (boundary)."""
        mock_conn.fetchrow.side_effect = [
            None,
            {"id": "11111111-1111-4111-8111-111111111111"},
        ]

        resp = client.post(self.REGISTER_URL, json={
            "email": "boundary@example.com",
            "password": "12345678",
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

    def test_register_missing_fields(self, client):
        """Should reject request without required fields."""
        resp = client.post(self.REGISTER_URL, json={})
        assert resp.status_code == 422


class TestLogin:
    """Tests for POST /auth/login."""

    LOGIN_URL = "/auth/login"

    def _mock_bcrypt_user(self, mock_conn, password="securePass123"):
        """Set up mock_conn with a bcrypt-hashed password that will pass verification."""
        from passlib.context import CryptContext
        pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
        hashed = pwd.hash(password)
        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "user@example.com",
            "password_hash": hashed,
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
