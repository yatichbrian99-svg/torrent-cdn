"""
Unit tests for file routes: list, get, get public, and delete.
"""
import json
from unittest.mock import AsyncMock
from uuid import UUID

from app.routes import files as files_route

FILE_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_FILE_ID = UUID("33333333-3333-4333-8333-333333333333")


def _make_file_row(fid=None):
    fid = fid or FILE_ID
    return {
        "id": fid,
        "filename": "test-video.mp4",
        "minio_url": f"http://minio:9000/bucket/key/{fid}.mp4",
        "torrent_magnet": "magnet:?xt=urn:btih:test",
        "file_size_mb": 10.5,
        "created_at": "2026-01-01T00:00:00Z",
    }


class TestListFiles:
    """Tests for GET /files."""

    LIST_URL = "/files"

    def test_list_files_empty(self, client, auth_headers, mock_conn):
        """Should return empty list when user has no files."""
        mock_conn.fetch.return_value = []

        resp = client.get(self.LIST_URL, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_files_with_results(self, client, auth_headers, mock_conn):
        """Should return list of user's files with analytics."""
        # First fetch returns files, second fetch returns analytics
        mock_conn.fetch.side_effect = [
            [_make_file_row()],  # file list
            [{"file_id": FILE_ID, "views": 5, "total_p2p": 800000, "total_origin": 200000}],  # analytics
        ]

        resp = client.get(self.LIST_URL, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["filename"] == "test-video.mp4"
        assert data[0]["file_size_mb"] == 10.5
        assert data[0]["views"] == 5
        assert data[0]["p2p_offload_percent"] == 80.0

    def test_list_files_no_auth(self, client):
        """Should reject without auth token (401)."""
        resp = client.get(self.LIST_URL)
        assert resp.status_code == 401


class TestGetFile:
    """Tests for GET /files/{file_id}."""

    def test_get_file_success(self, client, auth_headers, mock_conn):
        """Should return file details for owned file."""
        # First fetchrow = file lookup, second = analytics
        mock_conn.fetchrow.side_effect = [
            _make_file_row(),
            {"total_p2p": 800000, "total_origin": 200000, "views": 5},
        ]

        resp = client.get(f"/files/{FILE_ID}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "test-video.mp4"
        assert data["id"] == str(FILE_ID)
        assert data["views"] == 5
        assert data["p2p_offload_percent"] == 80.0

    def test_get_file_not_found(self, client, auth_headers, mock_conn):
        """Should return 404 for non-existent file."""
        mock_conn.fetchrow.return_value = None

        resp = client.get(f"/files/{OTHER_FILE_ID}", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"


class TestGetPublicFile:
    """Tests for GET /files/public/{file_id}."""

    PUBLIC_URL = "/files/public"

    def test_get_public_file_success(self, client, mock_conn):
        """Should return file info without auth (public endpoint)."""
        mock_conn.fetchrow.return_value = {
            "id": FILE_ID,
            "filename": "test-video.mp4",
            "minio_url": "http://minio:9000/bucket/key/test.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:test",
            "file_size_mb": 10.5,
        }

        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "test-video.mp4"
        assert "magnet_link" in data

    def test_get_public_file_not_found(self, client, mock_conn):
        """Should return 404 for non-existent public file."""
        mock_conn.fetchrow.return_value = None

        resp = client.get(f"{self.PUBLIC_URL}/{OTHER_FILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"

    def test_get_public_file_no_auth_required(self, client, mock_conn):
        """Should work without any auth headers (public)."""
        mock_conn.fetchrow.return_value = {
            "id": FILE_ID,
            "filename": "public-video.mp4",
            "minio_url": "http://minio:9000/bucket/key/test.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:test",
            "file_size_mb": 5.0,
        }

        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 200
        assert resp.json()["filename"] == "public-video.mp4"

    def test_get_public_file_served_from_redis_cache(self, client, mock_conn, mock_redis):
        """A cached payload is answered from Redis without touching the DB."""
        payload = {
            "id": str(FILE_ID),
            "filename": "test-video.mp4",
            "minio_url": "http://minio:9000/bucket/key/test.mp4",
            "magnet_link": "magnet:?xt=urn:btih:test",
            "file_size_mb": 10.5,
        }

        def get_side_effect(key):
            # Only the public-file cache key is warm; rate-limit keys stay cold.
            return json.dumps(payload) if key == f"public_file:{FILE_ID}" else None

        mock_redis.get.side_effect = get_side_effect
        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 200
        served = resp.json()
        # Read-time webseed annotation: a legacy cached magnet (no ws=) gains
        # the origin webseed on serve. The infohash is untouched (ws= is not
        # part of the info dict) so the swarm never forks.
        expected = dict(payload)
        expected["magnet_link"] = payload["magnet_link"] + "&ws=" + __import__(
            "urllib.parse", fromlist=["quote"]
        ).quote(payload["minio_url"], safe="")
        assert served == expected
        # Shared-cache header: the public viewer hot path may be served by the
        # edge/CDN (short TTL, immutable payload).
        assert resp.headers["cache-control"].startswith("public, max-age=")
        mock_conn.fetchrow.assert_not_called()

    def test_get_public_file_populates_redis_cache_on_miss(self, client, mock_conn, mock_redis):
        """A cache miss serves from the DB and writes the payload for next time."""
        mock_conn.fetchrow.return_value = {
            "id": FILE_ID,
            "filename": "test-video.mp4",
            "minio_url": "http://minio:9000/bucket/key/test.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:test",
            "file_size_mb": 10.5,
        }

        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 200
        cache_sets = [
            c for c in mock_redis.setex.call_args_list
            if str(c.args[0]) == f"public_file:{FILE_ID}"
        ]
        assert len(cache_sets) == 1
        assert cache_sets[0].args[1] == files_route.PUBLIC_FILE_CACHE_TTL
        assert json.loads(cache_sets[0].args[2])["filename"] == "test-video.mp4"


class TestDeleteFile:
    """Tests for DELETE /files/{file_id}."""

    def test_delete_file_success(self, client, auth_headers, mock_conn, mock_s3_client):
        """Should delete a file and return success."""
        mock_conn.fetchrow.return_value = {
            "minio_url": "http://minio:9000/torrent-cdn-files/user123/file123/test.mp4",
        }

        resp = client.delete(f"/files/{FILE_ID}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["detail"] == "File deleted successfully"

    def test_delete_file_shared_object_not_deleted(self, client, auth_headers, mock_conn, mock_s3_client):
        """Content-dedup: another upload still references the same stored
        object — deleting THIS row must NOT delete the shared bytes."""
        mock_conn.fetchrow.return_value = {
            "minio_url": "http://minio:9000/torrent-cdn-files/user123/file123/test.mp4",
        }
        mock_conn.fetchval.return_value = 1  # another upload still references it

        resp = client.delete(f"/files/{FILE_ID}", headers=auth_headers)
        assert resp.status_code == 200
        mock_s3_client.delete_object.assert_not_called()

    def test_delete_file_last_reference_deletes_object(self, client, auth_headers, mock_conn, mock_s3_client):
        """When the deleted row is the LAST reference to the shared object, the
        object itself is removed from storage."""
        mock_conn.fetchrow.return_value = {
            "minio_url": "http://minio:9000/torrent-cdn-files/user123/file123/test.mp4",
        }
        mock_conn.fetchval.return_value = 0  # no remaining references

        resp = client.delete(f"/files/{FILE_ID}", headers=auth_headers)
        assert resp.status_code == 200
        mock_s3_client.delete_object.assert_called_once_with(
            Bucket="torrent-cdn-files", Key="user123/file123/test.mp4"
        )

    def test_delete_file_not_found(self, client, auth_headers, mock_conn):
        """Should return 404 when deleting non-existent file."""
        mock_conn.fetchrow.return_value = None

        resp = client.delete(f"/files/{OTHER_FILE_ID}", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"

    def test_delete_file_no_auth(self, client):
        """Should reject deletion without auth token (401)."""
        resp = client.delete(f"/files/{FILE_ID}")
        assert resp.status_code == 401


class TestPoolSaturation:
    """Saturation behavior: when the asyncpg pool is exhausted past
    DB_POOL_ACQUIRE_TIMEOUT, the request gets a retryable 503 (via the
    asyncio.TimeoutError handler in main.py) — NOT a hang or a 500."""

    def test_db_acquire_timeout_returns_503(self, client, auth_headers, monkeypatch):

        class _ExhaustedPool:
            """Mimics a bounded pool.acquire() wait that expires: the wrapper in
            database.get_db_pool applies db_pool_acquire_timeout, and asyncpg
            raises asyncio.TimeoutError past it."""

            def acquire(self, *args, **kwargs):
                raise TimeoutError()

        monkeypatch.setattr(
            files_route, "get_db_pool", AsyncMock(return_value=_ExhaustedPool())
        )

        resp = client.get("/files", headers=auth_headers)
        assert resp.status_code == 503
        assert resp.headers.get("retry-after") == "5"
        assert "busy" in resp.json()["detail"].lower()

    def test_get_db_pool_wraps_raw_pool_in_timed_proxy(self, monkeypatch):
        """Regression: get_db_pool must return a proxy that injects
        db_pool_acquire_timeout into every acquire(). The previous
        implementation did ``pg_pool.acquire = wrapper`` in place, which raises
        AttributeError on asyncpg >= 0.30 ('Pool' object attribute 'acquire' is
        read-only — Cython __slots__). The exception was swallowed by
        run_startup_migrations' try/except, so the pool ran with asyncpg's
        DEFAULT acquire(), which blocks INDEFINITELY under saturation — the
        asyncio.TimeoutError → 503 handler never fired and requests surfaced
        as bare 500s. This test drives the REAL get_db_pool (via the reference
        main.py bound at import time, which conftest does not patch) against a
        mocked asyncpg.create_pool, so the wrapper actually has to install.
        """
        import asyncio
        import importlib

        from app import database
        # conftest's autouse apply_mocks replaces app.database.get_db_pool (and
        # therefore app.main's binding) with an AsyncMock BEFORE app.main is
        # imported by the client fixture — reload restores the real function so
        # this test drives the actual construction path. The autouse fixture
        # re-patches for the next test, so the reload is isolated to this test.
        database = importlib.reload(database)

        class _RawPool:
            def __init__(self):
                self.acquire_timeouts = []

            def acquire(self, *args, **kwargs):
                self.acquire_timeouts.append(kwargs.get("timeout"))
                return object()  # context-manager stand-in; unused here

            async def close(self):
                pass

        raw = _RawPool()
        monkeypatch.setattr(
            database.asyncpg, "create_pool", AsyncMock(return_value=raw)
        )
        monkeypatch.setattr(database, "pg_pool", None)

        pool = asyncio.run(database.get_db_pool())

        assert isinstance(pool, database._TimedPool), (
            "get_db_pool must return the timed proxy — returning a raw pool "
            "means the acquire timeout silently never applies"
        )
        # default timeout is injected when the caller passes none
        pool.acquire()
        assert raw.acquire_timeouts == [database.settings.db_pool_acquire_timeout]
        # an explicit timeout still wins
        pool.acquire(timeout=1.5)
        assert raw.acquire_timeouts == [
            database.settings.db_pool_acquire_timeout,
            1.5,
        ]
        # everything else delegates to the real pool (close, terminate, ...)
        assert pool.close == raw.close


class TestRedisFailOpen:
    """A dead/stale Redis client must fail OPEN, never 500 a request.

    Production incident: Render's free managed Redis was recycled, leaving the
    process with a CACHED client whose connections were dead. get_redis()
    handed it out without a liveness check, and the global RateLimitMiddleware's
    unguarded redis.get/incr/expire crashed EVERY non-exempt request (login
    included) into Starlette's plain-text 500 — which the frontend surfaced as
    the generic "Login failed. Please try again." (no JSON detail). The app is
    designed with in-memory fallbacks for exactly this case; the bug was that
    the failure mode never reached them.
    """

    def test_get_redis_creates_client_with_connection_hardening(self, monkeypatch):
        """The client Redis.from_url returns must be created with bounded socket
        timeouts, a health check, and a reconnect retry — the hardening that
        turns a dead/stuck managed-Redis connection into a fast error (→
        in-memory fallback) instead of an indefinite hang.
        """
        import asyncio
        import importlib

        from app import database
        database = importlib.reload(database)
        monkeypatch.setattr(database, "redis_client", None)

        captured = {}

        class _FakeClient:
            async def ping(self):
                return True

            async def aclose(self):
                pass

        async def _fake_from_url(url, **kwargs):
            captured["kwargs"] = kwargs
            return _FakeClient()

        monkeypatch.setattr(database.Redis, "from_url", _fake_from_url)

        asyncio.run(database.get_redis())

        kwargs = captured["kwargs"]
        assert kwargs.get("decode_responses") is True
        # socket timeouts bound every connect + command (redis-py defaults to
        # None = hang forever on a wedged connection)
        assert kwargs.get("socket_connect_timeout") == 5
        assert kwargs.get("socket_timeout") == 5
        # idle dead connections are PINGed out proactively before reuse
        assert kwargs.get("health_check_interval") == 30
        # reconnect retry is configured for connection errors only
        assert kwargs.get("retry") is not None
        assert kwargs.get("retry_on_error") == [database.ConnectionError]

    def test_get_redis_resets_stale_cached_client_and_fails_open(self, monkeypatch):
        """Drives the REAL get_redis: after a cached client's connection dies,
        the next call must tear it down and return None (callers then use their
        in-memory fallbacks) — and the call after that reconnects fresh.
        """
        import asyncio
        import importlib

        from app import database
        # Restore the real get_redis (conftest's autouse apply_mocks replaces it
        # with an AsyncMock); the autouse fixture re-patches for the next test.
        database = importlib.reload(database)
        monkeypatch.setattr(database, "redis_client", None)

        connect_count = {"n": 0}

        class _DeadAfterFirstPing:
            """A fake redis client: the first ping (connect-time) succeeds, then
            every later ping fails — mimicking a connection that died after the
            process cached the client (managed-Redis recycle/pause)."""

            def __init__(self):
                self.pings = 0
                self.closed = False

            async def ping(self):
                self.pings += 1
                if self.pings > 1:
                    raise ConnectionError("connection reset by peer")
                return True

            async def aclose(self):
                self.closed = True

        async def _fake_from_url(url, **kwargs):
            connect_count["n"] += 1
            return _DeadAfterFirstPing()

        monkeypatch.setattr(database.Redis, "from_url", _fake_from_url)

        # First call: connect + ping OK → client cached.
        first = asyncio.run(database.get_redis())
        assert first is not None
        assert database.redis_client is first
        assert connect_count["n"] == 1

        # Second call: cached client's ping fails (dead connection) → reset to
        # None and return None so callers fall back to in-memory. The dead
        # client is closed, not leaked.
        second = asyncio.run(database.get_redis())
        assert second is None
        assert database.redis_client is None
        assert first.closed is True

        # Third call: reconnects fresh (Redis back up).
        third = asyncio.run(database.get_redis())
        assert third is not None
        assert connect_count["n"] == 2

    def test_global_middleware_survives_dead_redis_client(self, client, monkeypatch):
        """End-to-end regression for the incident: when get_redis() returns a
        client whose commands raise (a stale cached client), the global
        RateLimitMiddleware must fall back to its in-memory limiter instead of
        500ing the request. /auth/me with no token answers 401 (route), NOT the
        plain-text 500 the frontend read as "Login failed.".
        """
        import app.database

        class _DeadClient:
            async def get(self, *a, **k):
                raise ConnectionError("connection reset by peer")

            async def incr(self, *a, **k):
                raise ConnectionError("connection reset by peer")

            async def expire(self, *a, **k):
                raise ConnectionError("connection reset by peer")

            async def ttl(self, *a, **k):
                raise ConnectionError("connection reset by peer")

        monkeypatch.setattr(
            app.database, "get_redis", AsyncMock(return_value=_DeadClient())
        )

        resp = client.get("/auth/me")
        # The middleware degraded to in-memory rate limiting; the request then
        # reached the route and was rejected for missing auth — a 401, never 500.
        assert resp.status_code == 401
