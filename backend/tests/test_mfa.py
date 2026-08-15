"""
Tests for two-factor authentication (TOTP):

1. Stdlib TOTP correctness (RFC 6238 vector) + window tolerance
2. Login second-factor gate (/auth/login → mfa_required → /auth/mfa/confirm)
3. Backup-code single-use consumption (replay fails)
4. Enable / disable flows (code required; sessions rotated)
5. MFA confirm rate limiting (5/15min per IP)
"""
import asyncio
import hashlib
import json
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.mfa import (
    backup_code_matches,
    create_mfa_token,
    decode_mfa_token,
    generate_backup_codes,
    generate_totp_secret,
    hash_backup_codes,
    totp_code,
    verify_totp,
)


class TestTotpPrimitives:
    """The stdlib TOTP implementation must be RFC 6238-correct."""

    # RFC 6238 Appendix B test secret: base32 of "12345678901234567890"
    RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def test_rfc6238_vector(self):
        """T=59 (counter 1), SHA-1, 6 digits → 287082 (RFC 6238 Appendix B)."""
        assert totp_code(self.RFC_SECRET, 59) == "287082"

    def test_generated_secret_is_base32(self):
        secret = generate_totp_secret()
        assert len(secret) == 32  # 20 random bytes → 32 base32 chars
        assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")

    def test_verify_accepts_current_code(self):
        secret = generate_totp_secret()
        assert verify_totp(secret, totp_code(secret)) is True

    def test_verify_rejects_wrong_code(self):
        secret = generate_totp_secret()
        assert verify_totp(secret, "000000") is False

    def test_verify_tolerates_clock_drift_one_step(self):
        """A code generated 30s in the past must still verify (window=1)."""
        secret = generate_totp_secret()
        stale = totp_code(secret, __import__("time").time() - 30)
        assert verify_totp(secret, stale) is True

    def test_verify_rejects_non_digit_and_wrong_length(self):
        secret = generate_totp_secret()
        assert verify_totp(secret, "abcdef") is False
        assert verify_totp(secret, "12345") is False
        assert verify_totp(secret, "") is False


class TestBackupCodes:
    def test_generation_shape(self):
        codes = generate_backup_codes()
        assert len(codes) == 10
        assert all(len(c) == 10 for c in codes)
        assert len(set(codes)) == 10  # all distinct

    def test_only_hashes_stored_and_matchable(self):
        codes = generate_backup_codes()
        hashes = hash_backup_codes(codes)
        assert all(h != c for h, c in zip(hashes, codes))  # never plaintext
        assert all(backup_code_matches(hashes, c) for c in codes)

    def test_wrong_code_rejected(self):
        hashes = hash_backup_codes(generate_backup_codes())
        assert backup_code_matches(hashes, "WRONGCODE1") is False


class TestMfaToken:
    def test_roundtrip(self):
        token = create_mfa_token("11111111-1111-4111-8111-111111111111")
        assert decode_mfa_token(token) == "11111111-1111-4111-8111-111111111111"

    def test_regular_access_token_is_not_mfa_token(self):
        """A normal session token (no mfa_pending claim) must never pass the
        MFA step — prevents replaying a session token as an MFA token."""
        from app.auth import create_access_token
        token = create_access_token({"sub": "11111111-1111-4111-8111-111111111111"})
        assert decode_mfa_token(token) is None

    def test_mfa_token_is_not_a_session_token(self):
        """Regression: an mfa_token shares the signing secret with session
        tokens, so the access-token decode path MUST reject the mfa_pending
        claim — otherwise the password half alone authenticates as a session."""
        from app.auth import decode_access_token
        token = create_mfa_token("11111111-1111-4111-8111-111111111111")
        assert decode_access_token(token) is None

    def test_garbage_rejected(self):
        assert decode_mfa_token("not-a-jwt") is None


class TestLoginSecondFactor:
    LOGIN_URL = "/auth/login"
    CONFIRM_URL = "/auth/mfa/confirm"
    USER = "11111111-1111-4111-8111-111111111111"

    def _mfa_user(self, mock_conn, password="securePass123"):
        from app.auth import hash_password
        secret = generate_totp_secret()
        codes = generate_backup_codes()
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.USER),
            "email": "user@example.com",
            "password_hash": hash_password(password),
            "verified": True,
            "mfa_enabled": True,
        }
        return secret, codes

    def test_login_returns_mfa_required_not_token(self, client, mock_conn):
        self._mfa_user(mock_conn)
        resp = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("mfa_required") is True
        assert "mfa_token" in data
        assert "access_token" not in data  # password alone gets NO session token

    def test_confirm_with_wrong_code_rejected(self, client, mock_conn):
        self._mfa_user(mock_conn)
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()
        # Confirm needs the user's mfa_secret/backup row
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "mfa_secret": generate_totp_secret(),  # NOT the real secret
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes())),
        }
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": "123456",
        })
        assert resp.status_code == 401
        assert "Invalid authentication code" in resp.json()["detail"]

    def test_confirm_with_valid_totp_mints_token(self, client, mock_conn):
        secret, _ = self._mfa_user(mock_conn)
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "mfa_secret": secret,
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes())),
        }
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": totp_code(secret),
        })
        assert resp.status_code == 200
        assert resp.json()["access_token"]
        mock_conn.execute.assert_not_called()  # TOTP path never touches backups

    def test_confirm_with_backup_code_consumes_it(self, client, mock_conn):
        _, codes = self._mfa_user(mock_conn)
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "mfa_secret": generate_totp_secret(),
            "mfa_backup_codes": json.dumps(hash_backup_codes(codes)),
        }
        mock_conn.execute.return_value = "UPDATE 1"

        code = codes[0]
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": code,
        })
        assert resp.status_code == 200
        assert resp.json()["access_token"]
        # The prune UPDATE was issued with the used hash removed. Params are
        # (user_uuid, json_remaining_hashes, stored_raw) — the JSON is index 1.
        sql, params = mock_conn.execute.call_args.args[0], mock_conn.execute.call_args.args[1:]
        assert "mfa_backup_codes" in sql
        used_hash = hashlib.sha256(code.encode()).hexdigest()
        assert used_hash not in json.loads(params[1])
        assert used_hash in json.loads(mock_conn.fetchrow.return_value["mfa_backup_codes"])

        # Replay: the same code must now FAIL (atomic consume → UPDATE 0)
        mock_conn.execute.return_value = "UPDATE 0"
        resp2 = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": code,
        })
        assert resp2.status_code == 401

    def test_confirm_reports_codes_remaining_after_backup_use(self, client, mock_conn):
        """The confirm response carries how many codes are left after a backup
        login, so the UI can warn the user as they run out."""
        _, codes = self._mfa_user(mock_conn)
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "mfa_secret": generate_totp_secret(),
            "mfa_backup_codes": json.dumps(hash_backup_codes(codes)),
        }
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": codes[0],
        })
        assert resp.status_code == 200
        assert resp.json()["backup_codes_remaining"] == len(codes) - 1

    def test_confirm_with_last_backup_code_reports_zero(self, client, mock_conn):
        """Consuming the FINAL code reports 0 — the trigger for the UI hint:
        the user is out of recovery codes and must mint a fresh set."""
        self._mfa_user(mock_conn)
        code = generate_backup_codes(1)[0]
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "mfa_secret": generate_totp_secret(),
            "mfa_backup_codes": json.dumps(hash_backup_codes([code])),
        }
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": code,
        })
        assert resp.status_code == 200
        assert resp.json()["backup_codes_remaining"] == 0

    def test_totp_confirm_omits_backup_codes_remaining(self, client, mock_conn):
        """TOTP logins consume nothing — the field must be absent (not 0) so
        the UI never shows the last-code warning for an authenticator login."""
        secret, _ = self._mfa_user(mock_conn)
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "mfa_secret": secret,
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes())),
        }
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": totp_code(secret),
        })
        assert resp.status_code == 200
        assert resp.json()["access_token"]
        assert "backup_codes_remaining" not in resp.json()

    def test_confirm_with_expired_or_bogus_token(self, client, mock_conn):
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": "bogus", "code": "123456",
        })
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"]

    def test_mfa_token_cannot_authenticate_protected_endpoint(self, client):
        """Endpoint-level: an mfa_token presented as a Bearer session token 401s."""
        token = create_mfa_token(self.USER)
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "Invalid or expired token" in resp.json()["detail"]

    def test_successful_confirm_resets_failure_counter(self, client, mock_conn, mock_redis):
        """A valid code clears the IP's failed-attempt counter (mirrors login),
        so a user who mistyped codes then succeeded can't hit a false 429."""
        secret, _ = self._mfa_user(mock_conn)
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "mfa_secret": secret,
            "mfa_backup_codes": None,
        }
        mock_redis.get.side_effect = (
            lambda key: "4" if str(key) == "mfa_attempts:testclient" else None
        )

        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": totp_code(secret),
        })
        assert resp.status_code == 200
        mock_redis.delete.assert_any_call("mfa_attempts:testclient")

    def test_confirm_rate_limited_after_five_failures(self, client, mock_conn, mock_redis):
        self._mfa_user(mock_conn)
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()

        def fake_get(key):
            if str(key) == "mfa_attempts:testclient":
                return "5"
            return None
        mock_redis.get.side_effect = fake_get

        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": "123456",
        })
        assert resp.status_code == 429
        assert "Too many attempts" in resp.json()["detail"]


class TestMfaLifecycle:
    """Setup → enable (code proves possession) → disable (code required)."""

    def test_status(self, client, auth_headers, mock_conn):
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": False,
            "is_admin": True,
            "mfa_backup_codes": None,
        }
        resp = client.get("/auth/mfa", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "is_admin": True, "backup_codes_remaining": None}

    def test_status_reports_remaining_backup_codes(self, client, auth_headers, mock_conn):
        """The dashboard needs the live count of unused codes (the persistent
        warning fires when it hits zero)."""
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "is_admin": False,
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes(3))),
        }
        resp = client.get("/auth/mfa", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["backup_codes_remaining"] == 3

    def test_setup_returns_secret_uri_and_codes_once(self, client, auth_headers, mock_conn):
        mock_conn.fetchrow.return_value = {
            "email": "user@example.com", "mfa_enabled": False,
        }
        resp = client.post("/auth/mfa/setup", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["secret"]) == 32
        assert data["otpauth_uri"].startswith("otpauth://totp/TorrentCDN:")
        assert data["otpauth_uri"].find("secret=" + data["secret"]) != -1
        assert len(data["backup_codes"]) == 10
        # The pending secret must be persisted so enable() can verify against it
        sql = mock_conn.execute.call_args.args[0]
        assert "mfa_secret" in sql and "mfa_backup_codes" in sql

    def test_setup_blocked_when_already_enabled(self, client, auth_headers, mock_conn):
        mock_conn.fetchrow.return_value = {"email": "user@example.com", "mfa_enabled": True}
        resp = client.post("/auth/mfa/setup", headers=auth_headers)
        assert resp.status_code == 400

    def test_enable_requires_valid_code(self, client, auth_headers, mock_conn):
        secret = generate_totp_secret()
        mock_conn.fetchrow.return_value = {"mfa_enabled": False, "mfa_secret": secret}

        # Wrong code → 400, flag never flipped
        resp = client.post("/auth/mfa/enable", headers=auth_headers, json={"code": "000000"})
        assert resp.status_code == 400
        mock_conn.execute.assert_not_called()

        # Correct code → 200 + session rotation (pw_change marker)
        resp = client.post("/auth/mfa/enable", headers=auth_headers, json={"code": totp_code(secret)})
        assert resp.status_code == 200
        assert "enabled" in resp.json()["detail"]

    def test_enable_sends_notification_email(self, client, auth_headers, mock_conn, monkeypatch):
        """Enabling MFA from the dashboard emails the account owner — a forced
        (or hijacked) enrollment must never go unnoticed."""
        import app.routes.auth as auth_route
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(auth_route, "send_mfa_enabled_email", sent)

        secret = generate_totp_secret()
        mock_conn.fetchrow.return_value = {
            "email": "user@example.com",
            "mfa_enabled": False,
            "mfa_secret": secret,
        }
        resp = client.post("/auth/mfa/enable", headers=auth_headers, json={"code": totp_code(secret)})
        assert resp.status_code == 200
        # Background tasks run synchronously under TestClient — assert the send.
        sent.assert_called_once_with("user@example.com")

    def test_enable_wrong_code_sends_no_email(self, client, auth_headers, mock_conn, monkeypatch):
        """A failed enable attempt must not trigger the notification."""
        import app.routes.auth as auth_route
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(auth_route, "send_mfa_enabled_email", sent)

        secret = generate_totp_secret()
        mock_conn.fetchrow.return_value = {
            "email": "user@example.com",
            "mfa_enabled": False,
            "mfa_secret": secret,
        }
        resp = client.post("/auth/mfa/enable", headers=auth_headers, json={"code": "000000"})
        assert resp.status_code == 400
        sent.assert_not_called()

    def test_disable_requires_code_and_wipes_secret(self, client, auth_headers, mock_conn):
        secret = generate_totp_secret()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": True,
            "mfa_secret": secret,
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes())),
        }
        resp = client.post("/auth/mfa/disable", headers=auth_headers, json={"code": "000000"})
        assert resp.status_code == 400  # wrong code can't strip MFA

        resp = client.post("/auth/mfa/disable", headers=auth_headers, json={"code": totp_code(secret)})
        assert resp.status_code == 200
        # The wipe UPDATE and the security_stamp rotation bump are separate
        # calls — search all of them (the stamp bump is issued last).
        sqls = [str(c.args[0]).lower() for c in mock_conn.execute.call_args_list]
        assert any("mfa_enabled = false" in s and "mfa_secret = null" in s for s in sqls)
        assert any("security_stamp" in s for s in sqls)


class TestMandatoryMfa:
    """MFA required for EVERY account (the production default, MFA_REQUIRED).

    Login never mints a session for an MFA-less user: it answers mfa_required +
    needs_setup with a short-lived mfa_token, and the client must enroll via
    /auth/mfa/setup (mfa_token auths the call — no session exists yet) then
    /auth/mfa/confirm, which verifies the TOTP, flips mfa_enabled, and mints
    the real session token in the same response.
    """

    LOGIN_URL = "/auth/login"
    CONFIRM_URL = "/auth/mfa/confirm"
    SETUP_URL = "/auth/mfa/setup"
    USER = "11111111-1111-4111-8111-111111111111"

    @pytest.fixture(autouse=True)
    def _require_mfa(self, monkeypatch):
        # Flip the flag on the cached Settings instance — every get_settings()
        # caller (login gate, confirm enrollment) sees the same object.
        from app.config import get_settings
        monkeypatch.setattr(get_settings(), "mfa_required", True)

    def _user_row(self, mfa_enabled=False, password="securePass123"):
        from app.auth import hash_password
        return {
            "id": UUID(self.USER),
            "email": "user@example.com",
            "password_hash": hash_password(password),
            "verified": True,
            "mfa_enabled": mfa_enabled,
        }

    def _login(self, client, mock_conn, password="securePass123"):
        mock_conn.fetchrow.return_value = self._user_row(mfa_enabled=False, password=password)
        return client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": password,
        })

    def test_login_gates_mfa_less_user_with_needs_setup(self, client, mock_conn):
        resp = self._login(client, mock_conn)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("mfa_required") is True
        assert data.get("needs_setup") is True
        assert "mfa_token" in data
        assert "access_token" not in data  # password alone gets NO session

    def test_login_with_mfa_enabled_has_needs_setup_false(self, client, mock_conn):
        mock_conn.fetchrow.return_value = self._user_row(mfa_enabled=True)
        resp = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        })
        data = resp.json()
        assert data.get("mfa_required") is True
        assert data.get("needs_setup") is False

    def test_setup_accepts_mfa_token_no_session(self, client, mock_conn):
        """Mid-login enrollment: the mfa_token alone (no session) may call setup."""
        login = self._login(client, mock_conn).json()
        mock_conn.fetchrow.return_value = {"email": "user@example.com", "mfa_enabled": False}
        resp = client.post(
            self.SETUP_URL,
            headers={"Authorization": f"Bearer {login['mfa_token']}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["secret"]) == 32
        assert len(data["backup_codes"]) == 10
        # Pending secret persisted so confirm can verify possession.
        sql = mock_conn.execute.call_args.args[0]
        assert "mfa_secret" in sql and "mfa_backup_codes" in sql

    def test_confirm_enrolls_user_and_mints_session(self, client, mock_conn):
        """Full required-enrollment flow: login → setup → confirm(TOTP) → session."""
        login = self._login(client, mock_conn).json()
        # Setup mints + persists the pending secret; grab it from the UPDATE.
        mock_conn.fetchrow.return_value = {"email": "user@example.com", "mfa_enabled": False}
        resp = client.post(
            self.SETUP_URL,
            headers={"Authorization": f"Bearer {login['mfa_token']}"},
        )
        assert resp.status_code == 200
        secret = mock_conn.execute.call_args.args[1]

        # Confirm reads the pending row and must enroll + mint the session.
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": False,
            "mfa_secret": secret,
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes())),
        }
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": totp_code(secret),
        })
        assert resp.status_code == 200
        assert resp.json()["access_token"]
        # Enrollment consumes nothing — the last-code field must be absent.
        assert "backup_codes_remaining" not in resp.json()

        # mfa_enabled flipped TRUE, and pre-MFA sessions rotated (stamp bump).
        sqls = [str(c.args[0]).lower() for c in mock_conn.execute.call_args_list]
        assert any("mfa_enabled = true" in s for s in sqls)
        assert any("security_stamp" in s for s in sqls)

    def test_confirm_enrollment_wrong_code_rejected(self, client, mock_conn):
        login = self._login(client, mock_conn).json()
        secret = generate_totp_secret()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": False,
            "mfa_secret": secret,
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes())),
        }
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": "000000",
        })
        assert resp.status_code == 401
        assert "Invalid authentication code" in resp.json()["detail"]
        # The flag was never flipped.
        sqls = [str(c.args[0]).lower() for c in mock_conn.execute.call_args_list]
        assert not any("mfa_enabled = true" in s for s in sqls)

    def test_confirm_enrollment_without_setup_rejected(self, client, mock_conn):
        """A user who skipped /auth/mfa/setup has no pending secret — no enroll."""
        login = self._login(client, mock_conn).json()
        mock_conn.fetchrow.return_value = {
            "mfa_enabled": False,
            "mfa_secret": None,
            "mfa_backup_codes": None,
        }
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": totp_code(generate_totp_secret()),
        })
        assert resp.status_code == 401

    def test_login_not_gated_when_mfa_not_required(self, client, mock_conn, monkeypatch):
        """Dev default (MFA_REQUIRED unset): an MFA-less login is NOT gated."""
        from app.config import get_settings
        monkeypatch.setattr(get_settings(), "mfa_required", False)
        mock_conn.fetchrow.return_value = self._user_row(mfa_enabled=False)
        resp = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        })
        assert resp.status_code == 200
        assert resp.json().get("access_token")  # plain session, no gate

    def test_token_types_are_not_interchangeable(self, client, mock_conn):
        """An mfa_token must NOT authenticate as a session on other endpoints,
        and a session token must still work for dashboard setup."""
        from app.auth import create_access_token
        session = create_access_token({"sub": self.USER})
        # mfa_token on a protected endpoint still 401s (existing invariant).
        mfa = self._login(client, mock_conn).json()
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {mfa['mfa_token']}"})
        assert resp.status_code == 401
        # Dashboard-style setup with a real session keeps working.
        mock_conn.fetchrow.return_value = {"email": "user@example.com", "mfa_enabled": False}
        resp = client.post(self.SETUP_URL, headers={"Authorization": f"Bearer {session}"})
        assert resp.status_code == 200

    def test_forced_enrollment_sends_notification_email(self, client, mock_conn, monkeypatch):
        """The mid-login forced enrollment emails the owner immediately — the
        exact case this notification exists for."""
        import app.routes.auth as auth_route
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(auth_route, "send_mfa_enabled_email", sent)

        login = self._login(client, mock_conn).json()
        # Setup mints + persists the pending secret; grab it from the UPDATE.
        mock_conn.fetchrow.return_value = {"email": "user@example.com", "mfa_enabled": False}
        resp = client.post(
            self.SETUP_URL,
            headers={"Authorization": f"Bearer {login['mfa_token']}"},
        )
        assert resp.status_code == 200
        secret = mock_conn.execute.call_args.args[1]

        mock_conn.fetchrow.return_value = {
            "email": "user@example.com",
            "mfa_enabled": False,
            "mfa_secret": secret,
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes())),
        }
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": totp_code(secret),
        })
        assert resp.status_code == 200
        assert resp.json()["access_token"]
        sent.assert_called_once_with("user@example.com")

    def test_existing_mfa_confirm_login_sends_no_email(self, client, mock_conn, monkeypatch):
        """Confirming login on an ALREADY-enrolled account must not email — 2FA
        wasn't just enabled, so the notification would be noise."""
        import app.routes.auth as auth_route
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(auth_route, "send_mfa_enabled_email", sent)

        mock_conn.fetchrow.return_value = self._user_row(mfa_enabled=True)
        login = client.post(self.LOGIN_URL, json={
            "email": "user@example.com", "password": "securePass123",
        }).json()
        secret = generate_totp_secret()
        mock_conn.fetchrow.return_value = {
            "email": "user@example.com",
            "mfa_enabled": True,
            "mfa_secret": secret,
            "mfa_backup_codes": None,
        }
        resp = client.post(self.CONFIRM_URL, json={
            "mfa_token": login["mfa_token"], "code": totp_code(secret),
        })
        assert resp.status_code == 200
        assert resp.json()["access_token"]
        sent.assert_not_called()


class TestRegistrationEnrollment:
    """Mandatory 2FA at registration: a brand-new account is born with MFA on.

    register answers the SAME gate as login — mfa_required + needs_setup with
    a short-lived mfa_token and NO session token — so the client walks the
    authenticator enrollment (setup → confirm) before any session exists.
    """

    REGISTER_URL = "/auth/register"
    SETUP_URL = "/auth/mfa/setup"
    CONFIRM_URL = "/auth/mfa/confirm"
    USER = UUID("11111111-1111-4111-8111-111111111111")

    @pytest.fixture(autouse=True)
    def _require_mfa(self, monkeypatch):
        from app.config import get_settings
        monkeypatch.setattr(get_settings(), "mfa_required", True)

    def _register(self, client, mock_conn, email="newuser@example.com"):
        # fetchrow sequence: (1) existing-user lookup → None, (2) INSERT RETURNING
        # id → the new user.
        mock_conn.fetchrow.side_effect = [
            None,
            {"id": self.USER},
        ]
        return client.post(self.REGISTER_URL, json={
            "email": email,
            "password": "SecurePass123",
            "terms_accepted": True,
        })

    def test_register_gates_new_account_with_needs_setup(self, client, mock_conn):
        resp = self._register(client, mock_conn)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("mfa_required") is True
        assert data.get("needs_setup") is True
        assert "mfa_token" in data
        assert "access_token" not in data  # no session before 2FA exists

    def test_register_not_gated_when_mfa_not_required(self, client, mock_conn, monkeypatch):
        """Dev default (MFA_REQUIRED unset): registration keeps minting a plain
        session token so local flows behave exactly as before."""
        from app.config import get_settings
        monkeypatch.setattr(get_settings(), "mfa_required", False)

        resp = self._register(client, mock_conn)
        assert resp.status_code == 200
        assert resp.json().get("access_token")

    def test_full_signup_enrollment_flow(self, client, mock_conn):
        """register → setup (via mfa_token) → confirm → session + mfa_enabled:
        the complete signup journey, in one flow."""
        resp = self._register(client, mock_conn)
        assert resp.status_code == 200
        mfa_token = resp.json()["mfa_token"]

        # The register-side side_effect iterator is spent — clear it so the
        # setup/confirm lookups use the return_value below.
        mock_conn.fetchrow.side_effect = None
        # Setup mints + persists the pending secret, authenticated by the
        # mfa_token (the new user has no session yet).
        mock_conn.fetchrow.return_value = {"email": "newuser@example.com", "mfa_enabled": False}
        setup = client.post(
            self.SETUP_URL,
            headers={"Authorization": f"Bearer {mfa_token}"},
        )
        assert setup.status_code == 200
        assert len(setup.json()["secret"]) == 32
        assert len(setup.json()["backup_codes"]) == 10
        secret = mock_conn.execute.call_args.args[1]

        # Confirm verifies the TOTP, flips the flag, and mints the session.
        mock_conn.fetchrow.return_value = {
            "email": "newuser@example.com",
            "mfa_enabled": False,
            "mfa_secret": secret,
            "mfa_backup_codes": json.dumps(hash_backup_codes(generate_backup_codes())),
        }
        confirm = client.post(self.CONFIRM_URL, json={
            "mfa_token": mfa_token, "code": totp_code(secret),
        })
        assert confirm.status_code == 200
        assert confirm.json()["access_token"]

        # The account is born MFA-enabled; pre-MFA sessions rotated (stamp).
        sqls = [str(c.args[0]).lower() for c in mock_conn.execute.call_args_list]
        assert any("mfa_enabled = true" in s for s in sqls)
        assert any("security_stamp" in s for s in sqls)


class TestMfaEnabledEmailSender:
    """Direct tests for send_mfa_enabled_email itself (the route-level tests
    above monkeypatch it away, so the real sender needs its own coverage)."""

    def test_sends_via_resend_with_correct_params(self, monkeypatch):
        import app.email_service as es
        fake_resend = MagicMock()
        monkeypatch.setattr(es, "_get_resend", lambda: fake_resend)
        monkeypatch.setattr(es.settings, "app_url", "https://torrentcdn.store")

        result = asyncio.run(es.send_mfa_enabled_email("user@example.com"))
        assert result is True
        params = fake_resend.Emails.send.call_args.args[0]
        assert params["to"] == ["user@example.com"]
        assert params["from"]
        assert "Two-factor authentication enabled" in params["subject"]
        # Body carries the security framing + a real dashboard link.
        assert "Didn't do this?" in params["html"]
        assert "https://torrentcdn.store/dashboard" in params["html"]
        assert "user@example.com" in params["html"]

    def test_returns_false_without_error_when_unconfigured(self, monkeypatch):
        import app.email_service as es
        monkeypatch.setattr(es, "_get_resend", lambda: None)
        # Must never raise — the enable already succeeded upstream.
        assert asyncio.run(es.send_mfa_enabled_email("user@example.com")) is False

    def test_returns_false_without_error_on_send_failure(self, monkeypatch):
        import app.email_service as es
        fake_resend = MagicMock()
        fake_resend.Emails.send.side_effect = Exception("smtp down")
        monkeypatch.setattr(es, "_get_resend", lambda: fake_resend)
        assert asyncio.run(es.send_mfa_enabled_email("user@example.com")) is False
