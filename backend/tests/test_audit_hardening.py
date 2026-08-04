"""
Regression tests for the full security-audit hardening pass.

Covers:
1. passlib → direct bcrypt (unmaintained dependency removed; $2b$ format kept)
2. bcrypt 72-byte password limit (multi-byte UTF-8 cannot silently truncate)
3. Input length caps (email/password/token/filename/ref_code) — huge inputs can
   no longer poison Redis rate-limit keys, blow up bcrypt memory, or 500 the
   VARCHAR(255) INSERTs
4. Login timing oracle (unknown email still performs a bcrypt verify)
5. Bounded in-memory rate-limit stores (no unbounded growth under rotated XFF)
6. Dead CORS origins removed (torrentcdn.com is no longer owned)
7. CSP-report logging sanitized against log injection
"""
import time
from io import BytesIO
from unittest.mock import AsyncMock
from uuid import UUID

import pytest


class TestBcryptPasswordHashing:
    """passlib removed — hashing uses the bcrypt library directly, keeping the
    standard $2b$ format so existing stored hashes remain verifiable."""

    def test_hash_verify_roundtrip(self):
        from app.auth import hash_password, verify_password

        h = hash_password("SecurePass123")
        assert h.startswith("$2b$")
        assert h != "SecurePass123"
        assert verify_password("SecurePass123", h)
        assert not verify_password("WrongPass123", h)

    def test_hash_is_salted(self):
        """Two hashes of the same password differ (random salt per call)."""
        from app.auth import hash_password

        assert hash_password("SecurePass123") != hash_password("SecurePass123")

    def test_verify_rejects_malformed_hash(self):
        """A corrupt stored hash must yield a mismatch, never crash the login path."""
        from app.auth import verify_password

        assert not verify_password("SecurePass123", "not-a-bcrypt-hash")
        assert not verify_password("SecurePass123", "")

    def test_hash_rejects_over_72_bytes(self):
        """bcrypt's hard input limit is 72 BYTES. A 73-byte password must raise
        before hashing, so two distinct long passwords can never collide via
        silent truncation."""
        from app.auth import hash_password

        with pytest.raises(ValueError, match="72-byte"):
            hash_password("a" * 73)

    def test_legacy_passlib_hash_still_verifies(self):
        """Hashes written by the old passlib backend ($2b$bcrypt) must verify
        with the direct-bcrypt implementation — no password reset required."""
        from app.auth import verify_password
        import bcrypt

        legacy = bcrypt.hashpw(b"SecurePass123", bcrypt.gensalt(rounds=12)).decode()
        assert legacy.startswith("$2b$")
        assert verify_password("SecurePass123", legacy)


class TestPasswordByteLimit:
    """Passwords longer than 72 bytes (multi-byte UTF-8) are rejected with a
    clear 400 at register/reset, never silently truncated."""

    def test_register_rejects_multibyte_over_72_bytes(self, client):
        # 30 emoji (4 bytes each = 120 bytes) but only 30 characters — the
        # pydantic 72-CHAR cap passes, so the byte check must catch it.
        pwd = "\U0001F600" * 30
        resp = client.post("/auth/register", json={
            "email": "emoji@example.com",
            "password": pwd,
            "terms_accepted": True,
        })
        assert resp.status_code == 400
        assert "72 bytes" in resp.json()["detail"]

    def test_reset_rejects_multibyte_over_72_bytes(self, client):
        pwd = "\U0001F600" * 30
        resp = client.post("/auth/reset-password", json={
            "token": "x" * 100,
            "password": pwd,
        })
        assert resp.status_code == 400
        assert "72 bytes" in resp.json()["detail"]

    def test_register_accepts_72_ascii_bytes(self, client, mock_conn):
        """Boundary: exactly 72 bytes is legal."""
        mock_conn.fetchrow.side_effect = [
            None,  # no existing user
            {"id": "11111111-1111-4111-8111-111111111111"},
        ]
        resp = client.post("/auth/register", json={
            "email": "boundary72@example.com",
            "password": "a" * 64 + "abc12345",  # 72 chars exactly
            "terms_accepted": True,
        })
        assert resp.status_code == 200


class TestInputLengthCaps:
    """Oversized attacker-controlled strings are rejected by pydantic (422)
    BEFORE they reach rate-limit keys, bcrypt, or VARCHAR columns."""

    def test_huge_email_rejected_register(self, client):
        resp = client.post("/auth/register", json={
            "email": "a" * 300 + "@example.com",
            "password": "SecurePass123",
            "terms_accepted": True,
        })
        assert resp.status_code == 422

    def test_huge_email_rejected_login(self, client):
        """The email is embedded in the Redis rate-limit key — a 300-char email
        would otherwise create multi-KB Redis keys per attempt."""
        resp = client.post("/auth/login", json={
            "email": "a" * 300 + "@example.com",
            "password": "SecurePass123",
        })
        assert resp.status_code == 422

    def test_huge_password_rejected(self, client):
        resp = client.post("/auth/register", json={
            "email": "user@example.com",
            "password": "A" * 5000 + "1",
            "terms_accepted": True,
        })
        assert resp.status_code == 422

    def test_huge_reset_token_rejected(self, client):
        resp = client.post("/auth/reset-password", json={
            "token": "A" * 10000,
            "password": "SecurePass123",
        })
        assert resp.status_code == 422

    def test_huge_ref_code_rejected(self, client):
        resp = client.post("/auth/register", json={
            "email": "user@example.com",
            "password": "SecurePass123",
            "terms_accepted": True,
            "ref_code": "A" * 100,
        })
        assert resp.status_code == 422

    def test_huge_filename_rejected_presign(self, client, auth_headers):
        resp = client.post("/upload/presign", json={
            "filename": "a" * 1000 + ".mp4",
            "file_size": 1024,
            "content_type": "video/mp4",
        }, headers=auth_headers)
        assert resp.status_code == 422

    def test_huge_filename_rejected_chunk_session(self, client, auth_headers):
        resp = client.post("/upload/session", json={
            "filename": "a" * 1000 + ".mp4",
            "file_size": 1024,
            "total_chunks": 1,
            "content_type": "video/mp4",
        }, headers=auth_headers)
        assert resp.status_code == 422

    def test_huge_idempotency_key_rejected(self, client, auth_headers):
        resp = client.post("/upload/presign", json={
            "filename": "clip.mp4",
            "file_size": 1024,
            "content_type": "video/mp4",
            "idempotency_key": "k" * 200,
        }, headers=auth_headers)
        assert resp.status_code == 422

    def test_normal_inputs_accepted(self, client, auth_headers, mock_conn, mock_s3_client):
        """Caps must not reject legitimate inputs — a normal presign flows through
        the dedup + usage check and mints a URL (same mock chain as the presign
        suite: dedup -> user -> usage)."""
        TEST_USER = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "plan": "free",
        }
        mock_conn.fetchrow.side_effect = [None, TEST_USER, None]
        mock_s3_client.generate_presigned_url.return_value = "https://storage.example/presigned-put"

        resp = client.post("/upload/presign", json={
            "filename": "clip.mp4",
            "file_size": 1024,
            "content_type": "video/mp4",
            "idempotency_key": "idem-123",
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["upload_id"]
        assert resp.json()["presigned_url"] == "https://storage.example/presigned-put"

        # Clean up the in-memory session so it can't leak into other tests.
        import app.routes.upload as upload_route
        for uid, s in list(upload_route._presign_sessions.items()):
            if s.get("user_id") == TEST_USER["id"]:
                upload_route._presign_sessions.pop(uid, None)


class TestLoginTimingOracle:
    """An unknown email performs a bcrypt verify against a fixed dummy hash, so
    response timing cannot distinguish registered from unregistered emails."""

    def test_unknown_user_returns_401_after_dummy_verify(self, client, mock_conn, monkeypatch):
        from app.auth import verify_password
        import app.routes.auth as auth_route

        mock_conn.fetchrow.return_value = None

        verified = []
        real_verify = verify_password

        def _spy(pwd, h):
            verified.append(h)
            return real_verify(pwd, h)

        monkeypatch.setattr(auth_route, "verify_password", _spy)

        resp = client.post("/auth/login", json={
            "email": "ghost@example.com",
            "password": "SecurePass123",
        })
        assert resp.status_code == 401
        # The dummy path MUST have run a bcrypt verify (not an early return).
        assert len(verified) == 1
        assert verified[0].startswith("$2b$")

    def test_known_user_wrong_password_same_verify_shape(self, client, mock_conn, monkeypatch):
        """Existing-user wrong-password path also does exactly one verify."""
        from app.auth import verify_password
        import app.routes.auth as auth_route
        from app.auth import hash_password

        mock_conn.fetchrow.return_value = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": "user@example.com",
            "password_hash": hash_password("SecurePass123"),
            "verified": True,
        }

        verified = []
        real_verify = verify_password

        def _spy(pwd, h):
            verified.append(h)
            return real_verify(pwd, h)

        monkeypatch.setattr(auth_route, "verify_password", _spy)

        resp = client.post("/auth/login", json={
            "email": "user@example.com",
            "password": "WrongPass123",
        })
        assert resp.status_code == 401
        assert len(verified) == 1


class TestBoundedRateLimitStores:
    """In-memory rate-limit fallbacks must drop expired keys so an attacker
    rotating X-Forwarded-For values cannot grow memory without bound."""

    def test_global_middleware_prunes_expired_keys_when_large(self, client, monkeypatch):
        """Once the in-memory store outgrows the threshold, keys whose whole
        window has expired are dropped — rotated-XFF abuse can't grow memory
        without bound. Fresh keys are untouched (no false eviction)."""
        import app.database
        monkeypatch.setattr(app.database, "get_redis", AsyncMock(return_value=None))

        from app.main import RateLimitMiddleware
        mw = self._find_rate_limit_middleware(client.app)
        assert mw is not None

        saved = dict(mw._memory_store)
        mw._memory_store.clear()
        try:
            # Force a prune cycle: shrink the threshold so a small store triggers it.
            mw._prune_threshold = 2
            old = time.time() - mw._window - 10
            fresh = time.time()
            mw._memory_store["203.0.113.1"] = [old]
            mw._memory_store["203.0.113.2"] = [old, old]
            mw._memory_store["203.0.113.3"] = [fresh]  # still within window

            resp = client.get("/files")
            assert resp.status_code == 401  # route ran, counted by middleware

            # Expired-only keys dropped; the live key survives.
            assert "203.0.113.1" not in mw._memory_store
            assert "203.0.113.2" not in mw._memory_store
            assert "203.0.113.3" in mw._memory_store
        finally:
            mw._prune_threshold = 512
            mw._memory_store.clear()
            mw._memory_store.update(saved)

    @staticmethod
    def _find_rate_limit_middleware(app):
        from app.main import RateLimitMiddleware
        stack = getattr(app, "middleware_stack", None)
        depth = 0
        while stack is not None and depth < 20:
            if isinstance(stack, RateLimitMiddleware):
                return stack
            stack = getattr(stack, "app", None)
            depth += 1
        return None

    def test_resend_cooldowns_bounded(self):
        """Expired resend-verification cooldown entries are pruned on write."""
        import app.routes.verification as v

        saved = dict(v._resend_cooldowns)
        v._resend_cooldowns.clear()
        try:
            now = time.time()
            v._resend_cooldowns["user-1"] = now - 200  # long expired
            v._resend_cooldowns["user-2"] = now - 10   # still cooling down
            v._prune_resend_cooldowns(now)
            assert "user-1" not in v._resend_cooldowns
            assert "user-2" in v._resend_cooldowns
        finally:
            v._resend_cooldowns.clear()
            v._resend_cooldowns.update(saved)


class TestDeadCorsOriginsRemoved:
    """torrentcdn.com / api.torrentcdn.com are no longer owned — they must not
    appear in the CORS allow-list (allow_credentials=True + a stale origin an
    attacker can re-register = credentialed cross-origin reads)."""

    def test_no_dead_domains_in_cors_allowlist(self):
        from app.main import cors_origins

        assert "https://torrentcdn.com" not in cors_origins
        assert "https://api.torrentcdn.com" not in cors_origins
        # The live frontend domains remain.
        assert "https://www.torrentcdn.store" in cors_origins


class TestCspReportLogSanitization:
    """Attacker-controlled CSP report fields must be sanitized before logging
    so a crafted blocked_uri can't forge log lines."""

    def test_process_report_strips_control_chars(self):
        from app.routes.csp import _process_report

        evil = "https://evil.example/x\n[FAKE] injected log line\r\ny"
        report = {
            "type": "csp-violation",
            "body": {
                "documentURL": "https://www.torrentcdn.store/",
                "blockedURL": evil,
                "violatedDirective": "script-src",
                "sourceFile": "line1\nline2",
            },
        }
        with pytest.MonkeyPatch.context() as mp:
            captured = []
            import logging
            logger = logging.getLogger("app.routes.csp")

            class _Recorder(logging.Handler):
                def emit(self, record):
                    captured.append(record.getMessage())

            h = _Recorder()
            logger.addHandler(h)
            try:
                _process_report(report)
            finally:
                logger.removeHandler(h)

            joined = "\n".join(captured)
            # The payload's control characters must be neutralized: the attacker
            # text survives as an in-line string, but no injected newline
            # survives to forge a SECOND log line (the forged line can never
            # appear as its own line in the output).
            assert "evil.example/x" in joined
            assert not any(line.startswith("[FAKE]") for line in joined.splitlines())
