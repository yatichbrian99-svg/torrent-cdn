"""
Unit tests for the presigned direct-to-Supabase upload path — the PRIMARY
upload flow. The browser PUTs the file bytes straight to Supabase Storage via a
short-lived presigned URL (never through Render or the Vercel proxy); the
backend only issues the URL (/upload/presign) and later verifies + finalizes
(/upload/presign/finalize/{upload_id}), streaming the object back server-side
to run the same validation/torrent/thumbnail pipeline as every other path.
"""
import re
from io import BytesIO
from unittest.mock import AsyncMock
from uuid import UUID

import app.routes.upload as upload_route
from app.security import (
    estimate_compression_ratio as _real_estimate_compression_ratio,
)
from app.security import (
    validate_content_type as _real_validate_content_type,
)

# Capture the REAL validator functions at module import time (before any
# conftest autouse fixture patches them to always-pass lambdas).
from app.security import (
    validate_file_magic as _real_validate_file_magic,
)

PRESIGN_URL = "/upload/presign"
TEST_USER = {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"}
FILE_ID = UUID("22222222-2222-4222-8222-222222222222")
VALID_MP4 = b"\x00\x00\x00\x20ftypmp42"  # 16 bytes, matches .mp4 magic
REAL_INFOHASH = re.compile(r"^magnet:\?xt=urn:btih:[0-9a-f]{40}&dn=.+$")


def _cleanup_session(upload_id):
    """Remove the presign session left behind by a test."""
    upload_route._presign_sessions.pop(upload_id, None)


class TestPresign:
    def test_presign_returns_presigned_put_url(self, client, auth_headers, mock_conn, mock_s3_client):
        mock_conn.fetchrow.side_effect = [None, TEST_USER, None]  # dedup -> user -> usage
        mock_s3_client.generate_presigned_url.return_value = "https://storage.example/presigned-put"

        resp = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={
                "filename": "movie.mp4",
                "file_size": 1024 * 1024,
                "content_type": "video/mp4",
                "idempotency_key": "presign-key-1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["upload_id"]
        assert data["presigned_url"] == "https://storage.example/presigned-put"
        assert data["content_type"] == "video/mp4"
        assert data["expires_in"] == upload_route.PRESIGN_URL_TTL
        assert data["result"] is None
        # The presigned URL must be a PUT put_object for the user-scoped object key
        kwargs = mock_s3_client.generate_presigned_url.call_args.kwargs
        assert kwargs["ClientMethod"] == "put_object"
        assert kwargs["HttpMethod"] == "PUT"
        assert kwargs["Params"]["Bucket"] == upload_route.settings.minio_bucket
        assert kwargs["Params"]["Key"].startswith(f"{TEST_USER['id']}/")
        assert kwargs["Params"]["ContentType"] == "video/mp4"
        assert kwargs["ExpiresIn"] == upload_route.PRESIGN_URL_TTL
        _cleanup_session(data["upload_id"])

    def test_presign_ensures_bucket_with_browser_cors(self, client, auth_headers, mock_conn, mock_s3_client):
        """The presign path bootstraps the bucket + browser-CORS rules so the
        direct browser -> storage PUT isn't blocked by CORS preflight (the
        whole point of the presigned flow). Best-effort: a provider that can't
        set bucket CORS via the S3 API still uploads fine."""
        mock_conn.fetchrow.side_effect = [None, TEST_USER, None]
        mock_s3_client.generate_presigned_url.return_value = "https://storage.example/put"

        resp = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={"filename": "movie.mp4", "file_size": 1000, "idempotency_key": "cors-key"},
        )
        assert resp.status_code == 200
        cors_calls = [c for c in mock_s3_client.put_bucket_cors.call_args_list]
        assert cors_calls, "CORS rules were never sent to the bucket"
        rules = cors_calls[0].kwargs["CORSConfiguration"]["CORSRules"]
        assert set(rules[0]["AllowedMethods"]) == {"GET", "HEAD", "PUT"}
        assert "*" in rules[0]["AllowedOrigins"]  # presigned URL is the credential
        assert "Content-Type" in rules[0]["AllowedHeaders"]
        _cleanup_session(resp.json()["upload_id"])

    def test_presign_no_auth(self, client):
        resp = client.post(
            PRESIGN_URL,
            json={"filename": "movie.mp4", "file_size": 1000},
        )
        assert resp.status_code == 401

    def test_presign_rejects_unsupported_type(self, client, auth_headers):
        resp = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={"filename": "malware.exe", "file_size": 1000},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Unsupported file type"

    def test_presign_rejects_empty_file(self, client, auth_headers):
        resp = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={"filename": "empty.mp4", "file_size": 0},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "File is empty"

    def test_presign_rejects_oversized_file(self, client, auth_headers):
        resp = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={"filename": "big.mp4", "file_size": 3 * 1024 * 1024 * 1024},
        )
        assert resp.status_code == 413
        assert "File too large" in resp.json()["detail"]

    def test_presign_dedup_returns_existing(self, client, auth_headers, mock_conn):
        """A completed upload for the key returns as `result` — client skips the PUT."""
        existing_id = UUID("33333333-3333-4333-8333-333333333333")
        mock_conn.fetchrow.return_value = {
            "id": existing_id,
            "filename": "movie.mp4",
            "minio_url": "https://storage.example/bucket/user/movie.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:testhash",
            "file_size_mb": 1.0,
            "thumbnail_url": None,
        }
        resp = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={
                "filename": "movie.mp4",
                "file_size": 1024 * 1024,
                "idempotency_key": "done-key",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["upload_id"] is None
        assert data["result"]["file_id"] == str(existing_id)
        assert data["presigned_url"] == ""

    def test_presign_reuses_pending_session_for_same_key(self, client, auth_headers, mock_conn, mock_s3_client):
        """A retried presign with the same key reuses the upload_id (idempotent)."""
        mock_conn.fetchrow.side_effect = [None, TEST_USER, None, None, TEST_USER, None]
        mock_s3_client.generate_presigned_url.return_value = "https://storage.example/put"

        first = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={"filename": "movie.mp4", "file_size": 1000, "idempotency_key": "reuse-key"},
        ).json()
        second = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={"filename": "movie.mp4", "file_size": 1000, "idempotency_key": "reuse-key"},
        ).json()

        assert first["upload_id"] == second["upload_id"]
        # A fresh URL is minted each time (the old one may be near expiry)
        assert mock_s3_client.generate_presigned_url.call_count == 2
        _cleanup_session(first["upload_id"])

    def test_presign_exceeds_usage_limit(self, client, auth_headers, mock_conn):
        # No idempotency key → the dedup SELECT is skipped, so the first
        # fetchrow is the user row, then the usage record.
        mock_conn.fetchrow.side_effect = [TEST_USER, {"total_gb_used": 100.0}]
        resp = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={"filename": "movie.mp4", "file_size": 1024 * 1024},  # ~1 MB
        )
        assert resp.status_code == 402
        assert "Usage limit exceeded" in resp.json()["detail"]


class TestPresignFinalize:
    def _presign(self, client, auth_headers, mock_conn, mock_s3_client, size=None, key="presign-e2e"):
        """Presign and return the upload_id (consumes the first 3 fetchrow calls)."""
        if size is None:
            size = len(VALID_MP4)
        mock_s3_client.generate_presigned_url.return_value = "https://storage.example/put"
        resp = client.post(
            PRESIGN_URL,
            headers=auth_headers,
            json={"filename": "movie.mp4", "file_size": size, "idempotency_key": key},
        )
        assert resp.status_code == 200
        return resp.json()["upload_id"]

    def test_presign_finalize_end_to_end_produces_real_infohash(
        self, client, auth_headers, mock_conn, mock_s3_client
    ):
        """Browser PUT → finalize returns a REAL torrent infohash (regression for
        the fake `btih:<uuid>` magnets minted by the old Torrent.create() call)
        and does NOT re-upload the object to storage."""
        # presign: dedup(None) -> user -> usage.  finalize: dedup(None) -> user
        # -> usage -> content-dedup miss -> INSERT RETURNING id.
        mock_conn.fetchrow.side_effect = [
            None, TEST_USER, None,
            None, TEST_USER, None,
            None,  # content-dedup: no existing copy
            {"id": FILE_ID},
        ]
        upload_id = self._presign(client, auth_headers, mock_conn, mock_s3_client)

        # Simulate the browser's direct PUT having landed in Supabase Storage.
        mock_s3_client.head_object.return_value = {"ContentLength": len(VALID_MP4)}
        mock_s3_client.get_object.return_value = {"Body": BytesIO(VALID_MP4)}

        resp = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        # The DB record id IS the presign session id (the object key is
        # {user}/{upload_id}/{filename} — same convention as the direct path).
        assert data["file_id"] == upload_id
        assert data["filename"] == "movie.mp4"
        # THE regression: the magnet must carry a real 40-hex infohash derived
        # from the file bytes — not the old fallback `btih:<random-uuid>`.
        assert REAL_INFOHASH.match(data["magnet_link"]), f"fake magnet: {data['magnet_link']}"
        assert data["minio_url"].endswith(f"{TEST_USER['id']}/{upload_id}/movie.mp4")
        # The object was already in storage — the shared pipeline must NOT
        # re-upload it via multipart.
        mock_s3_client.create_multipart_upload.assert_not_called()
        _cleanup_session(upload_id)

    def test_finalize_content_dedup_reuses_existing_copy(self, client, auth_headers, mock_conn, mock_s3_client):
        """Byte-identical content stores once on the presign path too: the
        browser's freshly-PUT duplicate object is deleted from storage and the
        row points at the existing copy's URL."""
        existing_url = "https://storage.example/bucket/other-user/other-file/original.mp4"
        # presign: idempotency-dedup -> user -> usage.  finalize: idempotency-
        # dedup -> user -> usage -> content-dedup HIT -> INSERT RETURNING id.
        mock_conn.fetchrow.side_effect = [
            None, TEST_USER, None,
            None, TEST_USER, None,
            {"minio_url": existing_url},
            {"id": FILE_ID},
        ]
        upload_id = self._presign(client, auth_headers, mock_conn, mock_s3_client)

        mock_s3_client.head_object.return_value = {"ContentLength": len(VALID_MP4)}
        mock_s3_client.get_object.return_value = {"Body": BytesIO(VALID_MP4)}

        resp = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["minio_url"] == existing_url
        # The duplicate (the browser's direct PUT) was deleted — nothing is
        # re-uploaded via multipart either.
        key = f"{TEST_USER['id']}/{upload_id}/movie.mp4"
        mock_s3_client.delete_object.assert_called_once_with(
            Bucket=upload_route.settings.minio_bucket, Key=key
        )
        mock_s3_client.create_multipart_upload.assert_not_called()
        _cleanup_session(upload_id)

    def test_finalize_rejects_missing_object(self, client, auth_headers, mock_conn, mock_s3_client):
        # presign(3) + finalize dedup + finalize usage(user, usage)
        mock_conn.fetchrow.side_effect = [None, TEST_USER, None, None, TEST_USER, None]
        upload_id = self._presign(client, auth_headers, mock_conn, mock_s3_client)

        mock_s3_client.head_object.side_effect = Exception("NoSuchKey")

        resp = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert resp.status_code == 400
        assert "not found or expired" in resp.json()["detail"]
        _cleanup_session(upload_id)

    def test_finalize_rejects_size_mismatch(self, client, auth_headers, mock_conn, mock_s3_client):
        mock_conn.fetchrow.side_effect = [None, TEST_USER, None, None, TEST_USER, None]
        upload_id = self._presign(client, auth_headers, mock_conn, mock_s3_client, size=16)

        # The browser PUT a 5-byte object instead of the declared 16.
        mock_s3_client.head_object.return_value = {"ContentLength": 5}

        resp = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert resp.status_code == 400
        assert "size does not match" in resp.json()["detail"]
        # Garbage object is cleaned up so a retry starts fresh
        mock_s3_client.delete_object.assert_called_once()
        _cleanup_session(upload_id)

    def test_finalize_rejects_magic_mismatch_and_cleans_up(self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch):
        """A disguised binary that slips past presign is caught at finalize and
        the stored object is deleted (400 before any DB write)."""
        import app.routes.upload
        monkeypatch.setattr(app.routes.upload, "validate_file_magic", _real_validate_file_magic)
        monkeypatch.setattr(app.routes.upload, "validate_content_type", _real_validate_content_type)
        monkeypatch.setattr(app.routes.upload, "estimate_compression_ratio", _real_estimate_compression_ratio)

        # Presign must declare the ACTUAL size so the size check passes and the
        # magic-byte validation (the real check under test) is reached.
        bad_content = b"this is not an mp4 file at all"
        mock_conn.fetchrow.side_effect = [None, TEST_USER, None, None, TEST_USER, None]
        upload_id = self._presign(client, auth_headers, mock_conn, mock_s3_client, size=len(bad_content), key="presign-e2e")
        mock_s3_client.head_object.return_value = {"ContentLength": len(bad_content)}
        mock_s3_client.get_object.return_value = {"Body": BytesIO(bad_content)}

        resp = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert resp.status_code == 400
        assert "File header does not match expected format" in resp.json()["detail"]
        mock_s3_client.delete_object.assert_called_once()
        _cleanup_session(upload_id)

    def test_finalize_dedup_returns_existing_before_rate_limit(self, client, auth_headers, mock_conn, mock_s3_client, mock_redis, monkeypatch):
        """A finalize retry after a lost response returns the stored upload and
        does NOT consume the per-IP upload budget."""
        import app.routes.upload
        monkeypatch.setattr(app.routes.upload, "get_redis", AsyncMock(return_value=mock_redis))

        existing_id = UUID("33333333-3333-4333-8333-333333333333")
        # presign: dedup(None) -> user -> usage.  finalize dedup: the stored row.
        mock_conn.fetchrow.side_effect = [
            None, TEST_USER, None,
            {
                "id": existing_id,
                "filename": "movie.mp4",
                "minio_url": "https://storage.example/bucket/user/movie.mp4",
                "torrent_magnet": "magnet:?xt=urn:btih:testhash",
                "file_size_mb": 1.0,
                "thumbnail_url": None,
            },
        ]
        upload_id = self._presign(client, auth_headers, mock_conn, mock_s3_client, key="dedup-finalize")

        resp = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["file_id"] == str(existing_id)
        # The rate limiter never ran for the retried finalize
        rate_calls = [c for c in mock_redis.incr.call_args_list if "upload_rate" in str(c)]
        assert rate_calls == []
        _cleanup_session(upload_id)

    def test_finalize_lost_response_retry_returns_stored_upload(
        self, client, auth_headers, mock_conn, mock_s3_client
    ):
        """Cold-start signature (hit by the live probe): the first finalize
        COMPLETED — session deleted, uploads row inserted — but its response
        died at the proxy. The client's retry has no session and no key, only
        the upload_id; it must be answered from the uploads row (id ==
        upload_id), NOT a 404, and must not re-validate/re-upload the object."""
        stored = {
            "id": FILE_ID,
            "filename": "movie.mp4",
            "minio_url": "https://storage.example/bucket/user/movie.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:testhash",
            "file_size_mb": 1.0,
            "thumbnail_url": None,
        }
        mock_conn.fetchrow.return_value = stored

        resp = client.post(f"/upload/presign/finalize/{FILE_ID}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_id"] == str(FILE_ID)
        assert data["filename"] == "movie.mp4"
        # Authorization preserved: the lookup is scoped to the caller's user_id
        # (args = [sql, id, user_id] — a loose `in` match would also pass if the
        # arg order ever changed, so pin the positions).
        args = mock_conn.fetchrow.call_args.args
        assert args[1] == FILE_ID
        assert args[2] == TEST_USER["id"]
        # No storage round-trip on a retry — the object is already finalized
        mock_s3_client.head_object.assert_not_called()
        mock_s3_client.create_multipart_upload.assert_not_called()

    def test_finalize_lost_response_no_row_still_404s(self, client, auth_headers, mock_conn):
        """A session-less finalize whose upload_id has no uploads row is still a
        genuine 404 (a stale/cancelled attempt), not an oracle or a 500."""
        mock_conn.fetchrow.return_value = None
        resp = client.post(
            f"/upload/presign/finalize/{UUID('99999999-9999-4999-8999-999999999999')}",
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Upload session not found"

    def test_finalize_rejects_unknown_session(self, client, auth_headers):
        resp = client.post("/upload/presign/finalize/does-not-exist", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Upload session not found"


class TestTorrentGeneration:
    """The torrent/magnet built for every upload must be P2P-friendly: a clean
    (reproducible) infohash and tr= trackers so the embed player can discover
    peers. Both are regressions the 2-peer E2E suite depends on."""

    def test_clean_name_reproducible_infohash_and_tr_trackers(self, tmp_path, mock_s3_client):
        from torrentool.api import Torrent

        content = bytes(range(256)) * 2000  # 512 KiB, deterministic, > 32 KiB → 256 KiB pieces
        file_stream = BytesIO(content)
        safe_filename = "clip.mp3"  # non-video ext → thumbnail step skipped
        user_id = UUID("11111111-1111-4111-8111-111111111111")
        file_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

        magnet, _, content_hash = upload_route._torrent_and_thumbnail(
            file_stream, safe_filename, user_id, file_id, mock_s3_client
        )
        assert len(content_hash) == 64  # SHA-256 hex drives storage dedup

        # Real 40-hex infohash, clean dn, and every announce tracker present as tr=.
        m = re.match(r"^magnet:\?xt=urn:btih:([0-9a-f]{40})&dn=clip\.mp3(&tr=.*)$", magnet)
        assert m, f"magnet malformed: {magnet}"
        assert "wss%3A%2F%2Ftracker.webtorrent.dev" in magnet
        assert "wss%3A%2F%2Ftracker.openwebtorrent.com" in magnet

        # The infohash is REPRODUCIBLE from the file bytes + name (what a browser
        # seeder does with create-torrent). The old mkstemp temp-prefix naming
        # made this impossible — torrent names leaked `tmpXXXXX_` prefixes.
        f = tmp_path / safe_filename
        f.write_bytes(content)
        rebuilt = Torrent.create_from(str(f))
        assert str(rebuilt.info_hash) == m.group(1)
        assert rebuilt.name == safe_filename
