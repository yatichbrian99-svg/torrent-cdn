"""
Regression tests for the second security-audit pass (hardening round 2).

Covers:
1. DELETE /files/{id} actually deletes the storage object (the old code
   referenced an undefined `settings` module global — the NameError was
   swallowed by the broad except, so the DB row was removed but the object
   stayed publicly accessible).
2. POST /analytics/event (public, unauthenticated) stays rate-limited when
   Redis is down (in-memory fallback).
3. POST /auth/register is throttled per IP (account/email spam).
4. GET /files is capped by pagination (limit/offset).
5. Paystack webhook only grants plans on settled (successful) subscription charges
   (moved to test_billing.py — TestPaystackWebhookPaymentGate).
6. Password changes revoke previously-issued JWTs (pw_change marker + iat).
7. Chunked/presign upload sessions are capped per user (temp-disk/Redis DoS).
8. Email templates HTML-escape the user-controlled email address.
9. Emails are NFC-normalized at the API boundary (confusable-account vector).
"""
import time
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class TestDeleteFileRemovesStorageObject:
    """Regression: DELETE /files/{id} must remove the object from storage.

    Pre-fix the handler referenced the undefined `settings` name inside the
    storage-deletion try/except; the NameError was swallowed, the DB row was
    deleted, and the public object survived forever.
    """

    URL = "/files/22222222-2222-4222-8222-222222222222"

    def test_delete_calls_s3_delete_object_with_correct_key(self, client, auth_headers, mock_conn, mock_s3_client):
        mock_conn.fetchrow.return_value = {
            "minio_url": "https://proj.supabase.co/storage/v1/object/public/torrent-cdn-files/user123/file456/video.mp4",
        }

        resp = client.delete(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        # The storage object MUST be deleted with the extracted key.
        mock_s3_client.delete_object.assert_called_once_with(
            Bucket="torrent-cdn-files",
            Key="user123/file456/video.mp4",
        )
        # DB row deleted with ownership scoping.
        delete_calls = [c.args[0].lower() for c in mock_conn.execute.call_args_list]
        assert any("delete from uploads where id = $1 and user_id = $2" in sql for sql in delete_calls)

    def test_delete_with_minio_style_url(self, client, auth_headers, mock_conn, mock_s3_client):
        """Path-style MinIO URLs extract the same last-3-segment key."""
        mock_conn.fetchrow.return_value = {
            "minio_url": "http://localhost:9000/torrent-cdn-files/user123/file456/video.mp4",
        }

        resp = client.delete(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        mock_s3_client.delete_object.assert_called_once_with(
            Bucket="torrent-cdn-files",
            Key="user123/file456/video.mp4",
        )

    def test_delete_still_succeeds_when_storage_fails(self, client, auth_headers, mock_conn, mock_s3_client):
        """A storage failure must not block the DB delete (user's listing is
        correct; the production orphan sweep reclaims the object)."""
        mock_conn.fetchrow.return_value = {
            "minio_url": "http://localhost:9000/torrent-cdn-files/user123/file456/video.mp4",
        }
        mock_s3_client.delete_object.side_effect = Exception("storage down")

        resp = client.delete(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["detail"] == "File deleted successfully"
        mock_conn.execute.assert_called_once()


class TestAnalyticsEventRateLimitFallback:
    """POST /analytics/event is public and unauthenticated — it must be rate
    limited even when Redis is down (an attacker could otherwise flood the
    analytics table with unbounded inserts during a Redis outage)."""

    URL = "/analytics/event"

    def test_event_rate_limited_in_memory_fallback(self, client, monkeypatch):
        import app.routes.analytics as analytics_route
        monkeypatch.setattr(analytics_route, "get_redis", AsyncMock(return_value=None))

        saved = dict(analytics_route._analytics_event_requests)
        analytics_route._analytics_event_requests.clear()
        try:
            # Seed the in-memory store at the cap for this IP.
            analytics_route._analytics_event_requests["testclient"] = [time.time()] * 60
            resp = client.post(self.URL, json={
                "file_id": "22222222-2222-4222-8222-222222222222",
                "event_type": "peer_joined",
            })
            assert resp.status_code == 429
            assert "Rate limit exceeded" in resp.json()["detail"]
        finally:
            analytics_route._analytics_event_requests.clear()
            analytics_route._analytics_event_requests.update(saved)

    def test_event_below_limit_proceeds(self, client, monkeypatch):
        import app.routes.analytics as analytics_route
        monkeypatch.setattr(analytics_route, "get_redis", AsyncMock(return_value=None))

        saved = dict(analytics_route._analytics_event_requests)
        analytics_route._analytics_event_requests.clear()
        try:
            # A registered file passes the file-exists gate.
            mock_conn = AsyncMock()
            mock_conn.fetchval.return_value = UUID("22222222-2222-4222-8222-222222222222")
            pool = AsyncMock()
            pool.acquire = MagicMock(return_value=pool)
            pool.__aenter__ = AsyncMock(return_value=mock_conn)
            pool.__aexit__ = AsyncMock(return_value=None)
            monkeypatch.setattr(analytics_route, "get_db_pool", AsyncMock(return_value=pool))

            resp = client.post(self.URL, json={
                "file_id": "22222222-2222-4222-8222-222222222222",
                "event_type": "chunk_downloaded",
                "bytes_p2p": 1000,
            })
            assert resp.status_code == 200
            mock_conn.execute.assert_awaited_once()
        finally:
            analytics_route._analytics_event_requests.clear()
            analytics_route._analytics_event_requests.update(saved)


class TestRegisterRateLimit:
    """POST /auth/register is throttled per IP — each registration costs a
    bcrypt hash AND a verification email, both spam-friendly resources."""

    URL = "/auth/register"

    def test_register_limited_via_redis(self, client, mock_conn, mock_redis):
        mock_redis.get.side_effect = lambda key: "10" if key == "register_rate:testclient" else None

        resp = client.post(self.URL, json={
            "email": "spam@example.com",
            "password": "SecurePass123",
            "terms_accepted": True,
        })
        assert resp.status_code == 429
        assert "Too many registrations" in resp.json()["detail"]
        mock_conn.fetchrow.assert_not_called()  # no DB work on a throttled request

    def test_register_in_memory_fallback(self, client, monkeypatch):
        import app.routes.auth as auth_route
        monkeypatch.setattr(auth_route, "get_redis", AsyncMock(return_value=None))

        saved = dict(auth_route._register_requests)
        auth_route._register_requests.clear()
        try:
            auth_route._register_requests["testclient"] = [time.time()] * 10
            resp = client.post(self.URL, json={
                "email": "spam@example.com",
                "password": "SecurePass123",
                "terms_accepted": True,
            })
            assert resp.status_code == 429
            assert "Too many registrations" in resp.json()["detail"]
        finally:
            auth_route._register_requests.clear()
            auth_route._register_requests.update(saved)

    def test_register_below_limit_proceeds(self, client, mock_conn):
        mock_conn.fetchrow.side_effect = [
            None,  # no existing user
            {"id": "11111111-1111-4111-8111-111111111111"},  # insert result
        ]
        resp = client.post(self.URL, json={
            "email": "legit@example.com",
            "password": "SecurePass123",
            "terms_accepted": True,
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()


class TestListFilesPagination:
    """GET /files must be capped (unbounded queries let one user force an
    arbitrarily large response)."""

    URL = "/files"

    def test_default_limit_applied(self, client, auth_headers, mock_conn):
        mock_conn.fetch.return_value = []
        resp = client.get(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        sql = mock_conn.fetch.call_args.args[0].lower()
        assert "limit $2 offset $3" in sql

    def test_explicit_limit_offset(self, client, auth_headers, mock_conn):
        mock_conn.fetch.return_value = []
        resp = client.get(f"{self.URL}?limit=25&offset=50", headers=auth_headers)
        assert resp.status_code == 200
        args = mock_conn.fetch.call_args.args
        # args: (sql, user_id, limit, offset)
        assert args[2] == 25
        assert args[3] == 50

    def test_limit_capped_at_max(self, client, auth_headers, mock_conn):
        mock_conn.fetch.return_value = []
        resp = client.get(f"{self.URL}?limit=999999", headers=auth_headers)
        assert resp.status_code == 422  # Query le=500 enforces the cap


class TestSessionRevocationOnPasswordChange:
    """A password change must invalidate all previously-issued JWTs. The reset
    route records pw_change:{user_id} in Redis; get_current_user_id rejects any
    token whose iat predates the marker."""

    def test_token_issued_before_change_rejected(self, client, mock_conn, mock_redis):
        from app.auth import create_access_token

        old_token = create_access_token({"sub": str(USER_ID)})

        # The pw_change marker is newer than the token's iat.
        iat = old_token  # placeholder; we decode below
        import jwt as pyjwt
        from app.config import get_settings
        payload = pyjwt.decode(old_token, get_settings().jwt_secret, algorithms=["HS256"])
        mock_redis.get.side_effect = lambda key: (
            str(float(payload["iat"]) + 1000) if key == f"pw_change:{USER_ID}" else None
        )

        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {old_token}"})
        assert resp.status_code == 401
        assert "Session invalidated" in resp.json()["detail"]

    def test_fresh_token_after_change_accepted(self, client, mock_conn, mock_redis):
        """A token minted AFTER the change passes the revocation check."""
        from app.auth import create_access_token
        import jwt as pyjwt
        from app.config import get_settings

        fresh = create_access_token({"sub": str(USER_ID)})
        payload = pyjwt.decode(fresh, get_settings().jwt_secret, algorithms=["HS256"])
        mock_redis.get.side_effect = lambda key: (
            str(float(payload["iat"]) - 1000) if key == f"pw_change:{USER_ID}" else None
        )
        mock_conn.fetchrow.return_value = {
            "id": USER_ID,
            "email": "user@example.com",
            "plan": "free",
            "created_at": "2026-01-01T00:00:00Z",
        }

        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {fresh}"})
        assert resp.status_code == 200

    def test_reset_password_records_marker(self, client, mock_conn, mock_redis):
        """A successful reset records pw_change for the user."""
        from app.email_service import create_password_reset_token

        token = create_password_reset_token(USER_ID)

        async def _fake_get(key):
            return str(USER_ID) if str(key).startswith("reset_token:") else None

        mock_redis.get.side_effect = _fake_get
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post("/auth/reset-password", json={"token": token, "password": "NewPass456!"})
        assert resp.status_code == 200
        # pw_change marker was written (any call to set with the pw_change key)
        assert any(
            "pw_change:" in str(c.args[0]) for c in mock_redis.set.call_args_list
        )

    def test_reset_password_writes_db_security_stamp(self, client, mock_conn, mock_redis):
        """A successful reset also bumps the DB security_stamp — the authoritative
        marker that works when Redis is down (production has no Redis)."""
        from app.email_service import create_password_reset_token

        token = create_password_reset_token(USER_ID)

        async def _fake_get(key):
            return str(USER_ID) if str(key).startswith("reset_token:") else None

        mock_redis.get.side_effect = _fake_get
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.post("/auth/reset-password", json={"token": token, "password": "NewPass456!"})
        assert resp.status_code == 200
        assert any(
            "security_stamp" in str(c.args[0]) for c in mock_conn.execute.call_args_list
        )


class TestSessionRevocationWithoutRedis:
    """The DB security_stamp is the AUTHORITATIVE session-revocation marker:
    it must reject pre-change JWTs whenever Redis can't answer — absent (the
    production topology, no REDIS_URL), erroring, or up-but-markerless after a
    restart/eviction. Regression for the live MFA E2E run where 'enable' did
    not rotate the old session token."""

    def _token(self):
        from app.auth import create_access_token
        return create_access_token({"sub": str(USER_ID)})

    def _stamp(self, offset_seconds):
        from datetime import datetime, timezone
        import jwt as pyjwt
        from app.config import get_settings
        payload = pyjwt.decode(self._token(), get_settings().jwt_secret, algorithms=["HS256"])
        return datetime.fromtimestamp(
            float(payload["iat"]) + offset_seconds, tz=timezone.utc
        )

    def _override_stamp(self, monkeypatch, stamp):
        """Point get_user_security_stamp at a scripted stamp."""
        from unittest.mock import AsyncMock
        import app.auth as auth_mod
        monkeypatch.setattr(auth_mod, "get_user_security_stamp", AsyncMock(return_value=stamp))

    def test_redis_down_token_issued_before_stamp_rejected(self, client, monkeypatch):
        """Redis absent + DB stamp newer than iat → 401."""
        import app.auth as auth_mod
        from datetime import datetime, timezone
        import jwt as pyjwt
        from app.config import get_settings

        old_token = self._token()
        payload = pyjwt.decode(old_token, get_settings().jwt_secret, algorithms=["HS256"])
        monkeypatch.setattr(auth_mod, "get_redis", AsyncMock(return_value=None))
        self._override_stamp(
            monkeypatch,
            datetime.fromtimestamp(float(payload["iat"]) + 1000, tz=timezone.utc),
        )

        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {old_token}"})
        assert resp.status_code == 401
        assert "Session invalidated" in resp.json()["detail"]

    def test_redis_down_fresh_token_accepted(self, client, mock_conn, monkeypatch):
        """Redis absent + stamp OLDER than iat → token still valid."""
        import app.auth as auth_mod
        from datetime import datetime, timezone
        import jwt as pyjwt
        from app.config import get_settings

        fresh = self._token()
        payload = pyjwt.decode(fresh, get_settings().jwt_secret, algorithms=["HS256"])
        monkeypatch.setattr(auth_mod, "get_redis", AsyncMock(return_value=None))
        self._override_stamp(
            monkeypatch,
            datetime.fromtimestamp(float(payload["iat"]) - 1000, tz=timezone.utc),
        )
        # /auth/me's own user lookup must succeed.
        mock_conn.fetchrow.return_value = {
            "id": USER_ID, "email": "user@example.com", "plan": "free",
            "verified": True, "is_admin": False, "created_at": "2026-01-01T00:00:00Z",
        }

        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {fresh}"})
        assert resp.status_code == 200

    def test_redis_down_no_stamp_means_no_change(self, client, mock_conn, monkeypatch):
        """NULL security_stamp (never changed) → token valid with Redis down."""
        import app.auth as auth_mod
        monkeypatch.setattr(auth_mod, "get_redis", AsyncMock(return_value=None))
        self._override_stamp(monkeypatch, None)
        # /auth/me's own user lookup must succeed.
        mock_conn.fetchrow.return_value = {
            "id": USER_ID, "email": "user@example.com", "plan": "free",
            "verified": True, "is_admin": False, "created_at": "2026-01-01T00:00:00Z",
        }

        resp = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {self._token()}"}
        )
        assert resp.status_code == 200

    def test_redis_up_but_markerless_newer_stamp_rejected(self, client, mock_redis, monkeypatch):
        """Redis UP but with no pw_change marker (restart/eviction window) must
        STILL fall through to the DB stamp — otherwise the authoritative check
        is bypassed exactly when Redis is flaky."""
        from datetime import datetime, timezone
        import jwt as pyjwt
        from app.config import get_settings

        token = self._token()
        payload = pyjwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        # Redis is up (conftest mock) and returns NO marker.
        mock_redis.get.return_value = None
        self._override_stamp(
            monkeypatch,
            datetime.fromtimestamp(float(payload["iat"]) + 1000, tz=timezone.utc),
        )

        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "Session invalidated" in resp.json()["detail"]

    def test_mfa_enable_bumps_db_security_stamp(self, client, auth_headers, mock_conn):
        """Enabling MFA writes the DB stamp so pre-MFA tokens die without Redis."""
        from app.mfa import generate_totp_secret, totp_code

        secret = generate_totp_secret()
        mock_conn.fetchrow.return_value = {"mfa_enabled": False, "mfa_secret": secret}

        resp = client.post(
            "/auth/mfa/enable", headers=auth_headers, json={"code": totp_code(secret)}
        )
        assert resp.status_code == 200
        assert any(
            "security_stamp" in str(c.args[0]) for c in mock_conn.execute.call_args_list
        )


class TestSessionCaps:
    """Chunked + presign sessions are capped per user so a single account can't
    exhaust temp disk / Redis session storage."""

    def test_chunk_session_cap_enforced(self, client, auth_headers, mock_conn, monkeypatch):
        import app.routes.upload as upload_route

        # Seed the store at the cap for this user (in-memory dict is module state).
        saved = dict(upload_route._chunk_sessions)
        upload_route._chunk_sessions.clear()
        try:
            for i in range(upload_route.MAX_ACTIVE_CHUNK_SESSIONS_PER_USER):
                upload_route._chunk_sessions[f"seed-{i}"] = {
                    "user_id": USER_ID,
                    "filename": "x.mp4",
                    "total_chunks": 1,
                    "file_size": 100,
                    "idempotency_key": None,
                    "received": set(),
                    "path": None,
                    "created_at": time.time(),
                }
            # Usage check runs BEFORE the cap check: user row, then usage row.
            mock_conn.fetchrow.side_effect = [
                {"id": USER_ID, "plan": "free"},
                None,  # no usage record
            ]
            resp = client.post("/upload/session", json={
                "filename": "clip.mp4",
                "file_size": 1000,
                "total_chunks": 1,
                "content_type": "video/mp4",
            }, headers=auth_headers)
            assert resp.status_code == 429
            assert "Too many active upload sessions" in resp.json()["detail"]
        finally:
            upload_route._chunk_sessions.clear()
            upload_route._chunk_sessions.update(saved)

    def test_chunk_cannot_exceed_declared_size(self, client, auth_headers, mock_conn, monkeypatch):
        """A chunk whose write would extend the assembly file past the declared
        file_size is rejected (closes the small-file_size/large-total_chunks
        temp-disk hole)."""
        import app.routes.upload as upload_route

        saved = dict(upload_route._chunk_sessions)
        upload_route._chunk_sessions.clear()
        try:
            session_id = "session-size-guard"
            upload_route._chunk_sessions[session_id] = {
                "user_id": USER_ID,
                "filename": "clip.mp4",
                "total_chunks": 2,
                # Declared size is small (e.g. 100 bytes) — a 4MB chunk at
                # index 0 would blow far past it.
                "file_size": 100,
                "idempotency_key": None,
                "received": set(),
                "path": None,
                "created_at": time.time(),
            }
            resp = client.post(
                f"/upload/chunk/{session_id}/0",
                headers=auth_headers,
                files={"chunk": ("c", BytesIO(b"x" * (4 * 1024 * 1024)), "application/octet-stream")},
            )
            assert resp.status_code == 400
            assert "exceeds declared file size" in resp.json()["detail"]
            assert 0 not in upload_route._chunk_sessions[session_id]["received"]
        finally:
            upload_route._chunk_sessions.clear()
            upload_route._chunk_sessions.update(saved)

    def test_chunk_within_declared_size_accepted(self, client, auth_headers, mock_conn, monkeypatch):
        """Chunks that stay within the declared size pass the guard."""
        import app.routes.upload as upload_route

        saved = dict(upload_route._chunk_sessions)
        upload_route._chunk_sessions.clear()
        try:
            session_id = "session-size-ok"
            upload_route._chunk_sessions[session_id] = {
                "user_id": USER_ID,
                "filename": "clip.mp4",
                "total_chunks": 1,
                "file_size": 10 * 1024 * 1024,
                "idempotency_key": None,
                "received": set(),
                "path": None,
                "created_at": time.time(),
            }
            resp = client.post(
                f"/upload/chunk/{session_id}/0",
                headers=auth_headers,
                files={"chunk": ("c", BytesIO(b"x" * 1024), "application/octet-stream")},
            )
            assert resp.status_code == 200
            assert resp.json()["received"] == 1
        finally:
            upload_route._chunk_sessions.clear()
            upload_route._chunk_sessions.update(saved)

    def test_presign_session_cap_enforced(self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch):
        import app.routes.upload as upload_route

        saved = dict(upload_route._presign_sessions)
        upload_route._presign_sessions.clear()
        try:
            for i in range(upload_route.MAX_ACTIVE_PRESIGN_SESSIONS_PER_USER):
                upload_route._presign_sessions[f"seed-{i}"] = {
                    "user_id": USER_ID,
                    "filename": "x.mp4",
                    "file_size": 100,
                    "content_type": "video/mp4",
                    "idempotency_key": None,
                    "object_key": f"{USER_ID}/seed-{i}/x.mp4",
                    "created_at": time.time(),
                }
            mock_conn.fetchrow.side_effect = [None, {"id": USER_ID, "plan": "free"}, None]
            resp = client.post("/upload/presign", json={
                "filename": "clip.mp4",
                "file_size": 1000,
                "content_type": "video/mp4",
            }, headers=auth_headers)
            assert resp.status_code == 429
            assert "Too many pending uploads" in resp.json()["detail"]
        finally:
            upload_route._presign_sessions.clear()
            upload_route._presign_sessions.update(saved)


class TestEmailHtmlEscaping:
    """User-controlled email addresses must be HTML-escaped in email bodies."""

    def test_reset_email_escapes_email(self):
        from app.email_service import build_reset_email_html

        html = build_reset_email_html('"><script>alert(1)</script>@evil.com', "https://x/reset")
        assert "&lt;script&gt;" in html
        assert "<script>alert" not in html

    def test_verification_email_escapes_email(self):
        from app.email_service import build_verification_email_html

        html = build_verification_email_html('"><img src=x onerror=alert(1)>@evil.com', "https://x/verify")
        assert "&lt;img" in html
        assert "<img src=x" not in html


    def test_login_falls_back_to_normalized_for_new_accounts(self, client, mock_conn, monkeypatch):
        """A user who registered post-normalization with the NFC form can log
        in typing the decomposed form (raw miss → NFC hit)."""
        from app.auth import hash_password
        hashed = hash_password("SecurePass123")
        nfc = "caf\u00e9@example.com"

        # First fetchrow (raw, decomposed) → None; second (NFC) → the user.
        mock_conn.fetchrow.side_effect = [
            None,
            {
                "id": USER_ID,
                "email": nfc,
                "password_hash": hashed,
                "verified": True,
            },
        ]

        resp = client.post("/auth/login", json={
            "email": "cafe\u0301@example.com",  # decomposed input
            "password": "SecurePass123",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_login_raw_email_still_works_for_legacy_accounts(self, client, mock_conn):
        """A pre-normalization account stored in decomposed form still logs in
        when the user types the exact stored address (raw lookup hit)."""
        from app.auth import hash_password
        hashed = hash_password("SecurePass123")

        mock_conn.fetchrow.return_value = {
            "id": USER_ID,
            "email": "cafe\u0301@example.com",
            "password_hash": hashed,
            "verified": True,
        }

        resp = client.post("/auth/login", json={
            "email": "cafe\u0301@example.com",
            "password": "SecurePass123",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()


class TestEmailNfcNormalization:
    """Emails are NFC-normalized at the API boundary — visually identical
    addresses in different Unicode normalization forms must collapse to one."""

    def test_register_normalizes_email(self, client, mock_conn):
        from app.routes.auth import _normalize_email

        # e-acute composed (U+00E9) vs decomposed (U+0065 U+0301)
        composed = "caf\u00e9@example.com"
        decomposed = "cafe\u0301@example.com"
        assert _normalize_email(composed) == _normalize_email(decomposed)

    def test_register_stores_normalized_email(self, client, mock_conn):
        """The INSERT must receive the NFC-normalized email."""
        from app.routes.auth import _normalize_email

        mock_conn.fetchrow.side_effect = [
            None,  # no existing user
            {"id": "11111111-1111-4111-8111-111111111111"},
        ]
        resp = client.post("/auth/register", json={
            "email": "cafe\u0301@example.com",  # decomposed
            "password": "SecurePass123",
            "terms_accepted": True,
        })
        assert resp.status_code == 200
        # The INSERT fetchrow has args (sql, user_uuid, email, password_hash, referral_code)
        inserted_email = mock_conn.fetchrow.call_args_list[1].args[2]
        assert inserted_email == _normalize_email("caf\u00e9@example.com")
