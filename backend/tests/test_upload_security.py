"""
Security tests for the upload and files routes:

1. Upload magic-byte validation (real security.validate_file_magic — the conftest
   autouse fixture stubs validators to always-pass, so we re-enable the real ones)
2. Content-Type mismatch rejection (real security.validate_content_type)
3. Oversized-file rejection (HTTP 413)
4. Files route: UUID path validation (blocks traversal), public rate limiting (Redis path)
"""
from io import BytesIO
from unittest.mock import AsyncMock
from uuid import UUID

# Capture the REAL validator functions at module import time (before any conftest
# autouse fixture patches them to always-pass lambdas).
from app.security import (
    validate_file_magic as _real_validate_file_magic,
    validate_content_type as _real_validate_content_type,
    estimate_compression_ratio as _real_estimate_compression_ratio,
)

FILE_ID = UUID("22222222-2222-4222-8222-222222222222")

# A valid MP4 ftyp signature (matches FILE_SIGNATURES[".mp4"] at offset 0)
MP4_MAGIC = b"\x00\x00\x00\x20ftypmp42"


def _enable_real_validation(monkeypatch):
    """Point the upload route's local validator references back to the real functions."""
    import app.routes.upload
    monkeypatch.setattr(app.routes.upload, "validate_file_magic", _real_validate_file_magic)
    monkeypatch.setattr(app.routes.upload, "validate_content_type", _real_validate_content_type)
    monkeypatch.setattr(app.routes.upload, "estimate_compression_ratio", _real_estimate_compression_ratio)


class TestUploadMagicByteValidation:
    """Uploads are rejected when the file's magic bytes don't match the extension."""

    UPLOAD_URL = "/upload"

    def test_upload_magic_mismatch_rejected(self, client, auth_headers, mock_s3_client, monkeypatch):
        """A .mp4 whose content is not actually MP4 → 400 before storage is touched."""
        _enable_real_validation(monkeypatch)

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("fake.mp4", BytesIO(b"this is not an mp4 file at all"), "video/mp4")},
        )
        assert resp.status_code == 400
        assert "File header does not match expected format" in resp.json()["detail"]
        mock_s3_client.create_multipart_upload.assert_not_called()

    def test_upload_blocked_elf_signature_rejected(self, client, auth_headers, monkeypatch):
        """ELF binaries hidden behind a .mp4 name are blocked by signature."""
        _enable_real_validation(monkeypatch)

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("evil.mp4", BytesIO(b"\x7fELF" + b"\x00" * 32), "video/mp4")},
        )
        assert resp.status_code == 400
        assert "blocked format" in resp.json()["detail"]
        assert "ELF" in resp.json()["detail"]

    def test_upload_blocked_zip_signature_rejected(self, client, auth_headers, monkeypatch):
        """ZIP archives (a.k.a. zip-bomb carriers) are blocked even with a video name."""
        _enable_real_validation(monkeypatch)

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("archive.mp4", BytesIO(b"PK\x03\x04" + b"\x00" * 32), "video/mp4")},
        )
        assert resp.status_code == 400
        assert "ZIP" in resp.json()["detail"]

    def test_upload_valid_magic_passes(self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch):
        """A genuinely-MP4 file passes real magic validation end-to-end."""
        _enable_real_validation(monkeypatch)
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,  # no usage record
            None,  # content-dedup: no existing copy
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("real.mp4", BytesIO(MP4_MAGIC), "video/mp4")},
        )
        assert resp.status_code == 200
        assert "file_id" in resp.json()


class TestUploadContentType:
    """Uploads are rejected when the declared Content-Type doesn't match the extension."""

    UPLOAD_URL = "/upload"

    def test_content_type_mismatch_rejected(self, client, auth_headers, mock_s3_client, monkeypatch):
        """Declaring text/plain for a .mp4 → 400 before storage is touched."""
        _enable_real_validation(monkeypatch)

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("video.mp4", BytesIO(MP4_MAGIC), "text/plain")},
        )
        assert resp.status_code == 400
        assert "doesn't match expected type" in resp.json()["detail"]
        mock_s3_client.create_multipart_upload.assert_not_called()

    def test_content_type_with_parameters_passes(self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch):
        """Content-Type with parameters (e.g. codecs) is normalized and accepted."""
        _enable_real_validation(monkeypatch)
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,
            None,  # content-dedup: no existing copy
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("video.mp4", BytesIO(MP4_MAGIC), "video/mp4; codecs=avc1.42E01E")},
        )
        assert resp.status_code == 200


class TestUploadOversizedFile:
    """Files over the configured max size are rejected with 413."""

    UPLOAD_URL = "/upload"

    def test_oversized_file_rejected_413(self, client, auth_headers, mock_s3_client, monkeypatch):
        """A file larger than max_upload_size_mb → 413, no storage call."""
        _enable_real_validation(monkeypatch)

        # Shrink the limit so a small fixture file exceeds it.
        import app.routes.upload
        monkeypatch.setattr(app.routes.upload.settings, "max_upload_size_mb", 0.001)  # ~1 KB

        # High-entropy payload keeps the compression-ratio check from false-flagging.
        oversized = MP4_MAGIC + bytes(range(256)) * 100  # ~25 KB > 1 KB
        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("big.mp4", BytesIO(oversized), "video/mp4")},
        )
        assert resp.status_code == 413
        assert "File too large" in resp.json()["detail"]
        mock_s3_client.create_multipart_upload.assert_not_called()

    def test_undersized_file_passes(self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch):
        """A file under the (lowered) limit is accepted."""
        _enable_real_validation(monkeypatch)
        import app.routes.upload
        monkeypatch.setattr(app.routes.upload.settings, "max_upload_size_mb", 1.0)  # 1 MB

        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,
            None,  # content-dedup: no existing copy
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("small.mp4", BytesIO(MP4_MAGIC), "video/mp4")},
        )
        assert resp.status_code == 200


class TestUploadRateLimit:
    """POST /upload is throttled per IP via the Redis-backed limiter."""

    UPLOAD_URL = "/upload"

    def test_upload_rate_limited_via_redis_429(self, client, auth_headers, mock_conn, mock_redis, monkeypatch):
        """At the hourly per-IP cap the Redis limiter returns 429."""
        import app.routes.upload
        monkeypatch.setattr(app.routes.upload, "get_redis", AsyncMock(return_value=mock_redis))

        # Limiter fires after validation/usage checks — provide a user row so
        # the request reaches the storage-bound limiter.
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,  # no usage record
        ]

        def fake_get(key):
            if key == "upload_rate:testclient":
                return "10"
            return None

        mock_redis.get.side_effect = fake_get
        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test.mp4", BytesIO(b"test"), "video/mp4")},
        )
        assert resp.status_code == 429
        assert "Upload limit reached" in resp.json()["detail"]

    def test_upload_below_limit_proceeds(self, client, auth_headers, mock_conn, mock_s3_client, mock_redis, monkeypatch):
        """An upload under the hourly cap goes through the limiter untouched."""
        import app.routes.upload
        monkeypatch.setattr(app.routes.upload, "get_redis", AsyncMock(return_value=mock_redis))

        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,
            None,  # content-dedup: no existing copy
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]
        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test.mp4", BytesIO(b"test"), "video/mp4")},
        )
        assert resp.status_code == 200
        # The limiter incremented the hourly counter for this IP
        mock_redis.incr.assert_any_call("upload_rate:testclient")


class TestFilesRouteSecurity:
    """Files-route security: UUID validation and public-endpoint rate limiting."""

    PUBLIC_URL = "/files/public"

    def test_public_file_invalid_uuid_rejected_422(self, client):
        """Non-UUID ids can't reach the query — FastAPI path validation rejects them."""
        resp = client.get(f"{self.PUBLIC_URL}/not-a-uuid")
        assert resp.status_code == 422

    def test_public_file_traversal_blocked(self, client):
        """Path traversal attempts never reach a raw file lookup (blocked, no data leak).

        httpx normalizes '../' segments before the router sees them (404 for a path that
        no longer matches) or the non-UUID remainder is rejected (422). Either way the
        traversal must not return a file.
        """
        for attempt in ("..%2F..%2Fetc%2Fpasswd", "..%2F..%2Fetc%2Fshadow", "....//....//etc//passwd"):
            resp = client.get(f"{self.PUBLIC_URL}/{attempt}")
            assert resp.status_code in (404, 422), f"traversal '{attempt}' reached handler: {resp.status_code}"

    def test_public_file_rate_limited_via_redis_429(self, client, mock_redis, monkeypatch):
        """The Redis-backed public endpoint limiter (30/min) returns 429 at the cap.

        Note: conftest does NOT patch app.routes.files.get_redis (only auth/billing/
        analytics/csp), so this test patches it explicitly to the mock Redis.
        """
        import app.routes.files
        monkeypatch.setattr(app.routes.files, "get_redis", AsyncMock(return_value=mock_redis))

        def fake_get(key):
            if key == "public_file_rate:testclient":
                return "30"
            return None

        mock_redis.get.side_effect = fake_get
        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 429
        assert "Rate limit exceeded" in resp.json()["detail"]
