"""
Tests for two-factor authentication (TOTP):

1. Stdlib TOTP correctness (RFC 6238 vector) + window tolerance
2. Login second-factor gate (/auth/login → mfa_required → /auth/mfa/confirm)
3. Backup-code single-use consumption (replay fails)
4. Enable / disable flows (code required; sessions rotated)
5. MFA confirm rate limiting (5/15min per IP)
"""
import hashlib
import json
from uuid import UUID
from unittest.mock import AsyncMock

from app.mfa import (
    generate_backup_codes,
    generate_totp_secret,
    hash_backup_codes,
    backup_code_matches,
    create_mfa_token,
    decode_mfa_token,
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
        mock_conn.fetchrow.return_value = {"mfa_enabled": False, "is_admin": True}
        resp = client.get("/auth/mfa", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "is_admin": True}

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
        sql = mock_conn.execute.call_args.args[0].lower()
        assert "mfa_enabled = false" in sql
        assert "mfa_secret = null" in sql
