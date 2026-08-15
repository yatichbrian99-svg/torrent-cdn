"""
Unit tests for the orphaned-object sweep, the prune bug-fix, and Redis-backed
presign sessions.

- sweep_orphan_objects: deletes bucket objects with no uploads row AND no
  pending presign session AND older than the grace window. It is the single
  place storage objects get garbage-collected.
- _prune_presign_sessions regression: it used to delete the session's object on
  expiry — but finalize never removed the session, so ~30 min after a SUCCESSFUL
  upload the next presign/finalize call deleted a FINALIZED user file. Pruning
  must only drop the in-memory copy.
- Redis-backed presign sessions: a session survives a Render instance recycle
  (finalize reads it back from Redis after the in-memory copy is gone).
"""
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock
from uuid import UUID

import pytest

import app.routes.upload as upload_route
from app.security import (
    estimate_compression_ratio as _real_estimate_compression_ratio,
)
from app.security import (
    validate_content_type as _real_validate_content_type,
)

# Capture the REAL validator functions at module import time (before any conftest
# autouse fixture patches them to always-pass lambdas — importing them inside a
# test body would capture the patched lambda and silently skip validation).
from app.security import (
    validate_file_magic as _real_validate_file_magic,
)

TEST_USER = {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"}
VALID_MP4 = b"\x00\x00\x00\x20ftypmp42"
BUCKET = upload_route.settings.minio_bucket


def _obj(key, age_hours=2.0):
    """A fake ListObjectsV2 Contents entry, old enough to be past the grace window."""
    return {
        "Key": key,
        "LastModified": datetime.now(UTC) - timedelta(hours=age_hours),
    }


def _configure_listing(mock_s3_client, objects, truncated=False, next_token=None):
    resp = {"Contents": objects, "IsTruncated": truncated}
    if next_token:
        resp["NextContinuationToken"] = next_token
    mock_s3_client.list_objects_v2 = MagicMock(return_value=resp)


class TestOrphanSweep:
    @pytest.mark.asyncio
    async def test_sweep_deletes_orphan_object(self, mock_conn, mock_s3_client):
        key = f"{TEST_USER['id']}/22222222-2222-4222-8222-222222222222/orphan.mp4"
        _configure_listing(mock_s3_client, [_obj(key)])
        mock_conn.fetchrow.return_value = None  # no uploads row

        deleted = await upload_route.sweep_orphan_objects()

        assert deleted == 1
        mock_s3_client.delete_object.assert_called_once_with(Bucket=BUCKET, Key=key)

    @pytest.mark.asyncio
    async def test_sweep_keeps_object_referenced_by_dedup_copy(self, mock_conn, mock_s3_client):
        """Content-dedup regression: the ORIGINAL upload's row is gone (its
        owner deleted the file — the refcount-aware delete kept the shared
        bytes), but another upload still references the object via minio_url.
        The id check alone would see 'no row' and delete the shared bytes; the
        sweep must also check minio_url references before deleting."""
        file_id = "22222222-2222-4222-8222-222222222222"
        key = f"{TEST_USER['id']}/{file_id}/file.mp4"
        _configure_listing(mock_s3_client, [_obj(key)])
        # First fetchrow (id check) -> no row; second (minio_url refcount) -> hit
        mock_conn.fetchrow.side_effect = [
            None,
            {"id": UUID("33333333-3333-4333-8333-333333333333")},
        ]

        deleted = await upload_route.sweep_orphan_objects()

        assert deleted == 0
        mock_s3_client.delete_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_keeps_object_with_uploads_row(self, mock_conn, mock_s3_client):
        """A finalized upload's object is NEVER deleted — the sweep checks the
        uploads table (id == key's 2nd path segment) before deleting anything."""
        file_id = "22222222-2222-4222-8222-222222222222"
        key = f"{TEST_USER['id']}/{file_id}/file.mp4"
        _configure_listing(mock_s3_client, [_obj(key)])
        mock_conn.fetchrow.return_value = {"id": UUID(file_id)}

        deleted = await upload_route.sweep_orphan_objects()

        assert deleted == 0
        mock_s3_client.delete_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_failsafe_when_session_state_unknown(self, mock_conn, mock_s3_client, mock_redis):
        """A Redis read error must NEVER cause a deletion — the session check
        is fail-safe (skip), unlike a naive treat-miss-as-orphan which would
        delete an object whose session is merely temporarily unreadable."""
        key = f"{TEST_USER['id']}/22222222-2222-4222-8222-222222222222/file.mp4"
        _configure_listing(mock_s3_client, [_obj(key)])
        mock_conn.fetchrow.return_value = None
        mock_redis.get.side_effect = Exception("connection lost")

        deleted = await upload_route.sweep_orphan_objects()

        assert deleted == 0
        mock_s3_client.delete_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_keeps_object_with_pending_session(self, mock_conn, mock_s3_client):
        """An object whose presign session is still alive is left alone — the
        browser may be mid-finalize."""
        upload_id = "22222222-2222-4222-8222-222222222222"
        key = f"{TEST_USER['id']}/{upload_id}/file.mp4"
        _configure_listing(mock_s3_client, [_obj(key)])
        mock_conn.fetchrow.return_value = None
        upload_route._presign_sessions[upload_id] = {
            "user_id": TEST_USER["id"],
            "object_key": key,
            "created_at": time.time(),
        }
        try:
            deleted = await upload_route.sweep_orphan_objects()
        finally:
            upload_route._presign_sessions.pop(upload_id, None)

        assert deleted == 0
        mock_s3_client.delete_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_skips_young_objects_within_grace(self, mock_conn, mock_s3_client):
        """Objects younger than the grace window are untouched (in-flight uploads)."""
        key = f"{TEST_USER['id']}/22222222-2222-4222-8222-222222222222/new.mp4"
        _configure_listing(mock_s3_client, [_obj(key, age_hours=0.1)])  # 6 minutes old
        mock_conn.fetchrow.return_value = None

        deleted = await upload_route.sweep_orphan_objects()

        assert deleted == 0
        mock_s3_client.delete_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_paginates_through_truncated_listings(self, mock_conn, mock_s3_client):
        key1 = f"{TEST_USER['id']}/11111111-1111-4111-8111-111111111111/a.mp4"
        key2 = f"{TEST_USER['id']}/22222222-2222-4222-8222-222222222222/b.mp4"
        mock_s3_client.list_objects_v2 = MagicMock(
            side_effect=[
                {"Contents": [_obj(key1)], "IsTruncated": True, "NextContinuationToken": "tok1"},
                {"Contents": [_obj(key2)], "IsTruncated": False},
            ]
        )
        mock_conn.fetchrow.return_value = None

        deleted = await upload_route.sweep_orphan_objects()

        assert deleted == 2
        assert mock_s3_client.list_objects_v2.call_count == 2
        assert mock_s3_client.list_objects_v2.call_args_list[1].kwargs["ContinuationToken"] == "tok1"
        assert mock_s3_client.delete_object.call_count == 2

    @pytest.mark.asyncio
    async def test_sweep_ignores_non_upload_keys(self, mock_conn, mock_s3_client):
        _configure_listing(mock_s3_client, [_obj("not-an-upload-key")])
        mock_conn.fetchrow.return_value = None

        deleted = await upload_route.sweep_orphan_objects()

        assert deleted == 0
        mock_s3_client.delete_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_handles_empty_bucket(self, mock_conn, mock_s3_client):
        _configure_listing(mock_s3_client, [])
        deleted = await upload_route.sweep_orphan_objects()
        assert deleted == 0


class TestPruneNeverDeletesObjects:
    """Regression: the old _prune_presign_sessions deleted the session's object
    on expiry — but presign finalize never removed the session, so ~30 min after
    a SUCCESSFUL upload the next presign/finalize call deleted a FINALIZED user
    file. Pruning must only drop the in-memory copy; object GC is the sweep's
    job (it checks the uploads table first)."""

    def test_prune_drops_expired_session_without_touching_storage(self, mock_s3_client):
        upload_id = "22222222-2222-4222-8222-222222222222"
        upload_route._presign_sessions[upload_id] = {
            "user_id": TEST_USER["id"],
            "object_key": f"{TEST_USER['id']}/{upload_id}/file.mp4",
            "created_at": time.time() - upload_route.PRESIGN_SESSION_TTL - 10,
        }
        try:
            upload_route._prune_presign_sessions()
        finally:
            upload_route._presign_sessions.pop(upload_id, None)

        mock_s3_client.delete_object.assert_not_called()
        assert upload_id not in upload_route._presign_sessions


class TestRedisPresignSessions:
    def test_presign_finalize_survives_instance_restart_via_redis(
        self, client, auth_headers, mock_conn, mock_s3_client, mock_redis
    ):
        """The session lives in Redis: after presign, a fresh instance (memory
        wiped — the Render recycle case) still finalizes. The object key and
        declared size come from Redis, not from client-supplied metadata."""
        # presign: dedup(None) -> user -> usage.  finalize: dedup(None) ->
        # user -> usage -> content-dedup miss -> INSERT RETURNING id.
        mock_conn.fetchrow.side_effect = [
            None, TEST_USER, None,
            None, TEST_USER, None,
            None,
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]
        mock_s3_client.generate_presigned_url.return_value = "https://storage.example/put"

        resp = client.post(
            "/upload/presign",
            headers=auth_headers,
            json={"filename": "movie.mp4", "file_size": len(VALID_MP4), "idempotency_key": "redis-restart"},
        )
        assert resp.status_code == 200
        upload_id = resp.json()["upload_id"]

        # Capture what was written to Redis (session JSON + idx -> upload_id),
        # then simulate an instance recycle: wipe the in-memory copy.
        redis_store = {}
        for call in mock_redis.setex.call_args_list:
            key, _ttl, value = call.args
            redis_store[key] = value
        assert f"presign_session:{upload_id}" in redis_store
        upload_route._presign_sessions.clear()

        # The fresh instance reads the session back from Redis.
        mock_redis.get.side_effect = lambda key: redis_store.get(key)
        mock_s3_client.head_object.return_value = {"ContentLength": len(VALID_MP4)}
        mock_s3_client.get_object.return_value = {"Body": BytesIO(VALID_MP4)}

        finalize = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert finalize.status_code == 200
        assert finalize.json()["magnet_link"].startswith("magnet:?xt=urn:btih:")
        upload_route._presign_sessions.pop(upload_id, None)

    def test_finalize_rejection_consumes_session(
        self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch
    ):
        """A definitive rejection (400) consumes the session too — a later
        presign with the same idempotency key starts a fresh session/object key
        instead of reusing a dead one whose object was deleted."""
        monkeypatch.setattr(upload_route, "validate_file_magic", _real_validate_file_magic)
        monkeypatch.setattr(upload_route, "validate_content_type", _real_validate_content_type)
        monkeypatch.setattr(upload_route, "estimate_compression_ratio", _real_estimate_compression_ratio)

        bad_content = b"this is not an mp4 file at all"
        mock_conn.fetchrow.side_effect = [None, TEST_USER, None, None, TEST_USER, None]
        mock_s3_client.generate_presigned_url.return_value = "https://storage.example/put"
        resp = client.post(
            "/upload/presign",
            headers=auth_headers,
            json={"filename": "movie.mp4", "file_size": len(bad_content), "idempotency_key": "reject-clean"},
        )
        assert resp.status_code == 200
        upload_id = resp.json()["upload_id"]
        mock_s3_client.head_object.return_value = {"ContentLength": len(bad_content)}
        mock_s3_client.get_object.return_value = {"Body": BytesIO(bad_content)}

        finalize = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert finalize.status_code == 400
        assert upload_id not in upload_route._presign_sessions

    def test_finalize_removes_session_on_success(
        self, client, auth_headers, mock_conn, mock_s3_client, mock_redis
    ):
        """A successful finalize drops the session from both stores — so the
        idempotency-key index never points at a stale session and the sweep
        never has to second-guess it."""
        # No idempotency key → the dedup SELECT is skipped on both calls, so the
        # sequences are: presign(user, usage) + finalize(user, usage, content-
        # dedup miss, INSERT).
        mock_conn.fetchrow.side_effect = [
            TEST_USER, None,
            TEST_USER, None,
            None,  # content-dedup miss
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]
        mock_s3_client.generate_presigned_url.return_value = "https://storage.example/put"
        resp = client.post(
            "/upload/presign",
            headers=auth_headers,
            json={"filename": "movie.mp4", "file_size": len(VALID_MP4)},
        )
        assert resp.status_code == 200
        upload_id = resp.json()["upload_id"]
        mock_s3_client.head_object.return_value = {"ContentLength": len(VALID_MP4)}
        mock_s3_client.get_object.return_value = {"Body": BytesIO(VALID_MP4)}

        finalize = client.post(f"/upload/presign/finalize/{upload_id}", headers=auth_headers)
        assert finalize.status_code == 200
        assert upload_id not in upload_route._presign_sessions
        session_key_deletes = [
            c for c in mock_redis.delete.call_args_list
            if c.args and c.args[0] == f"presign_session:{upload_id}"
        ]
        assert session_key_deletes
