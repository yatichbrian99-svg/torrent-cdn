"""Tests for the live channel management API: CRUD, stream-key generation and
rotation, ownership scoping, and derived status."""
import asyncio
import hashlib
import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.live_models import hash_stream_key, new_slug, new_stream_key, stream_key_prefix
from app.mediamtx import mint_playback_token, verify_playback_token


class TestChannelCrud:
    URL = "/api/live/channels"

    def test_requires_auth(self, client):
        resp = client.get(self.URL)
        assert resp.status_code == 401

    def test_create_channel(self, client, auth_headers, mock_conn, test_user_id):
        mock_conn.fetch.return_value = []  # no existing slugs
        mock_conn.fetchrow.return_value = {
            "id": uuid4(), "tenant_id": test_user_id, "owner_id": test_user_id,
            "name": "My Stream", "slug": "my-stream", "description": None,
            "stream_key_prefix": "live_abcd…", "status": "OFFLINE", "enabled": True,
            "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
            "last_seen_at": None,
        }
        resp = client.post(self.URL, json={"name": "My Stream"}, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["slug"] == "my-stream"
        assert data["status"] == "OFFLINE"
        # OBS setup card: the plaintext key + paste-ready URLs are returned
        # EXACTLY ONCE here (creation), then stored hashed — never listable.
        assert data["stream_key"].startswith("live_")
        assert data["publish_path"] == "live/my-stream"
        assert data["rtmp_server"].startswith("rtmp://")
        # The stream key is stored hashed, never in plaintext
        sql = mock_conn.fetchrow.await_args.args[0]
        assert "stream_key_hash" in sql
        assert "stream_key_hash, stream_key_prefix" not in data.values()

    def test_create_generates_unique_slugs(self, client, auth_headers, mock_conn, test_user_id):
        mock_conn.fetch.return_value = [{"slug": "my-stream"}]
        captured = {}
        def fake_fetchrow(sql, *args):
            captured["args"] = args
            return {
                "id": uuid4(), "tenant_id": test_user_id, "owner_id": test_user_id,
                "name": "My Stream", "slug": args[2], "description": None,
                "stream_key_prefix": "p", "status": "OFFLINE", "enabled": True,
                "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
                "last_seen_at": None,
            }
        mock_conn.fetchrow.side_effect = fake_fetchrow
        resp = client.post(self.URL, json={"name": "My Stream"}, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.json()["slug"] == "my-stream-2"

    def test_list_scoped_to_owner(self, client, auth_headers, mock_conn):
        mock_conn.fetch.return_value = []
        resp = client.get(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        sql = mock_conn.fetch.await_args.args[0]
        assert "owner_id = $1" in sql

    def test_get_other_tenant_channel_is_404(self, client, auth_headers, mock_conn):
        mock_conn.fetchrow.return_value = None
        resp = client.get(f"{self.URL}/{uuid4()}", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_clears_ephemeral_state(self, client, auth_headers, mock_conn, test_user_id):
        ch = {
            "id": uuid4(), "tenant_id": test_user_id, "owner_id": test_user_id,
            "name": "C", "slug": "gone", "description": None,
            "stream_key_prefix": "p", "status": "OFFLINE", "enabled": True,
            "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
            "last_seen_at": None,
        }
        mock_conn.fetchrow.return_value = ch
        with patch("app.routes.live.clear_channel", new=AsyncMock()) as cc:
            resp = client.delete(f"{self.URL}/{ch['id']}", headers=auth_headers)
        assert resp.status_code == 200
        cc.assert_awaited_once_with("gone")


class TestStreamKeys:
    def test_key_format_and_entropy(self):
        k1, k2 = new_stream_key(), new_stream_key()
        assert k1.startswith("live_")
        assert k1 != k2
        assert len(k1) > 40

    def test_hash_is_sha256_of_plaintext(self):
        key = new_stream_key()
        assert hash_stream_key(key) == hashlib.sha256(key.encode()).hexdigest()

    def test_prefix_never_contains_secret(self):
        key = new_stream_key()
        prefix = stream_key_prefix(key)
        assert key not in prefix
        assert len(prefix) <= 12

    def test_rotate_key_returns_plaintext_once(self, client, auth_headers, mock_conn, test_user_id):
        ch_id = uuid4()
        mock_conn.fetchrow.return_value = {
            "id": ch_id, "tenant_id": test_user_id, "owner_id": test_user_id,
            "name": "C", "slug": "c", "description": None, "stream_key_prefix": "p",
            "status": "OFFLINE", "enabled": True,
            "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
            "last_seen_at": None,
        }
        resp = client.post(f"/api/live/channels/{ch_id}/rotate-key", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["stream_key"].startswith("live_")
        # Reveal/rotate also refreshes the paste-ready OBS URLs.
        assert data["publish_path"] == "live/c"
        assert data["rtmp_server"].startswith("rtmp://")
        # The UPDATE stores only the hash
        update_sql = mock_conn.execute.await_args.args[0]
        assert "stream_key_hash" in update_sql

    def test_slug_collision_suffix(self):
        existing = {"abc"}
        assert new_slug("ABC", existing) == "abc-2"
        existing.add("abc-2")
        assert new_slug("ABC", existing) == "abc-3"


class TestPlaybackTokens:
    def test_roundtrip(self):
        from uuid import uuid4
        cid = str(uuid4())
        token = mint_playback_token(cid, ttl_seconds=60)
        assert verify_playback_token(token, cid)

    def test_wrong_channel_rejected(self):
        from uuid import uuid4
        token = mint_playback_token(str(uuid4()), ttl_seconds=60)
        assert not verify_playback_token(token, str(uuid4()))

    def test_expired_rejected(self):
        from uuid import uuid4
        cid = str(uuid4())
        token = mint_playback_token(cid, ttl_seconds=-10)
        assert not verify_playback_token(token, cid)

    def test_tampered_rejected(self):
        from uuid import uuid4
        cid = str(uuid4())
        token = mint_playback_token(cid, ttl_seconds=60)
        payload, sig = token.split(".", 1)
        assert not verify_playback_token(f"{payload}.{'0' * len(sig)}", cid)

    def test_garbage_rejected(self):
        assert not verify_playback_token("not-a-token", "whatever")


class TestDerivedStatus:
    def _row(self, **over):
        base = {
            "id": uuid4(), "slug": "c", "enabled": True, "status": "OFFLINE",
            "last_seen_at": None,
        }
        base.update(over)
        return base

    async def _derive(self, row, *, mm_path=None, authoritative=False, meta=None):
        with patch("app.routes.live.resolve_ingress",
                   new=AsyncMock(return_value=(mm_path, authoritative))), \
             patch("app.routes.live.get_channel_meta", new=AsyncMock(return_value=meta or {})):
            from app.routes.live import derive_channel_status
            return await derive_channel_status(row)

    def test_disabled_is_offline(self):
        import asyncio
        assert asyncio.run(self._derive(self._row(enabled=False), mm_path={"ready": True})) == "OFFLINE"

    def test_mediamtx_ready_is_live(self):
        import asyncio
        assert asyncio.run(self._derive(self._row(), mm_path={"ready": True})) == "LIVE"

    def test_fresh_segment_is_live(self):
        import asyncio
        meta = {"last_seg_ts": str(__import__("time").time() - 1)}
        assert asyncio.run(self._derive(self._row(), meta=meta)) == "LIVE"

    def test_stale_segment_is_starting(self):
        import asyncio
        import time as t
        meta = {"last_seg_ts": str(t.time() - 20)}
        assert asyncio.run(self._derive(self._row(), meta=meta)) == "STARTING"

    def test_nothing_is_offline(self):
        import asyncio
        assert asyncio.run(self._derive(self._row())) == "OFFLINE"

    def test_mediamtx_down_falls_back_to_registry(self):
        # MediaMTX unreachable (authoritative=False) → segment registry decides
        import asyncio
        import time as t
        meta = {"last_seg_ts": str(t.time() - 1)}
        assert asyncio.run(self._derive(self._row(), meta=meta)) == "LIVE"

    def test_mediamtx_says_gone_is_offline_even_with_fresh_segments(self):
        # Regression: an authoritative "no such path" must be OFFLINE —
        # stale segment freshness must not pin a dead stream in STARTING.
        import asyncio
        import time as t
        meta = {"last_seg_ts": str(t.time() - 1)}
        assert asyncio.run(
            self._derive(self._row(), authoritative=True, meta=meta)) == "OFFLINE"


class TestSchemaMatchesQueries:
    """Regression guard for the prod 500: live_channels DDL lacked
    stream_key_prefix while every query selected/inserted it (tests mock the
    DB, so the mismatch only surfaced in production). Parses the SQL in
    routes/live.py and asserts every referenced column exists in the DDL."""

    def _ddl_columns(self, table: str) -> set[str]:
        import re

        from app.database import LIVE_TABLES_DDL
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\);", LIVE_TABLES_DDL, re.DOTALL)
        assert m, f"{table} DDL not found"
        cols = set()
        for line in m.group(1).splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(("CREATE INDEX", "UNIQUE(", "CONSTRAINT")):
                continue
            name = line.split()[0]
            if name.upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}:
                continue
            cols.add(name)
        return cols

    def _statements(self) -> list[str]:
        import re

        from app.routes import live as live_routes
        src = inspect.getsource(live_routes)
        return [m.group(1) for m in re.finditer(r'"""(SELECT|INSERT|UPDATE|DELETE)[^"]*"""', src, re.IGNORECASE)]

    def test_every_quoted_live_channels_column_exists_in_ddl(self):
        import re
        cols = self._ddl_columns("live_channels")
        for stmt in self._statements():
            if "live_channels" not in stmt:
                continue
            # SELECT lists: capture column tokens between SELECT and FROM
            for sel in re.finditer(r"SELECT(.*?)FROM", stmt, re.DOTALL | re.IGNORECASE):
                for tok in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", sel.group(1)):
                    name = tok.group(0)
                    if name.upper() in {"SELECT", "DISTINCT", "AS", "COUNT", "NOW", "NOW()", "FROM"}:
                        continue
                    assert name in cols, f"live_channels query references unknown column {name!r} in: {stmt[:120]}"
            # INSERT column lists
            for ins in re.finditer(r"INSERT INTO live_channels \((.*?)\)", stmt, re.DOTALL | re.IGNORECASE):
                for tok in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", ins.group(1)):
                    assert tok.group(0) in cols, f"INSERT references unknown column {tok.group(0)!r}"
            # UPDATE SET lists
            for upd in re.finditer(r"UPDATE live_channels SET(.*?)(?:WHERE|$)", stmt, re.DOTALL | re.IGNORECASE):
                for tok in re.finditer(r"SET\s*,?\s*|([A-Za-z_][A-Za-z0-9_]*)\s*=", upd.group(1)):
                    if tok.group(1):
                        assert tok.group(1) in cols, f"UPDATE references unknown column {tok.group(1)!r}"

    def test_stream_key_prefix_in_ddl(self):
        assert "stream_key_prefix" in self._ddl_columns("live_channels")


class TestSlugUniquenessGuard:
    """Slugs are looked up GLOBALLY (public playback, auth hook, sessions) —
    the schema must enforce a global unique slug, and the startup migration
    must keep enforcing it on databases created before the fix."""

    def test_ddl_enforces_global_unique_slug(self):
        import re

        from app.database import LIVE_TABLES_DDL
        m = re.search(r"CREATE TABLE IF NOT EXISTS live_channels \((.*?)\);", LIVE_TABLES_DDL, re.DOTALL)
        assert m, "live_channels DDL not found"
        assert re.search(r"UNIQUE\s*\(\s*slug\s*\)", m.group(1)), \
            "live_channels must declare UNIQUE(slug) — per-tenant scope lets slugs shadow each other"

    def test_startup_migration_creates_unique_slug_index(self):
        import re

        import app.main as main_mod
        with open(main_mod.__file__) as f:
            src = f.read()
        assert re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS idx_live_channels_slug ON live_channels\(slug\)", src), \
            "startup migration must add the global slug unique index for pre-existing databases"


class TestViewerRateLimit:
    """_check_viewer_rate_limit — per-IP-per-CHANNEL viewer budget.

    The player fires ~24 calls/min per viewer (heartbeat every 5s + availability
    reports in the same tick when P2P transfers are active). Sizing regression:
    the old default of 60/min tripped at the THIRD viewer behind one shared IP
    (family wifi / church / office NAT — exactly the sunday-service audience):
    session joins 429'd silently, viewers vanished from the dashboard, and lost
    P2P entirely (join failure is swallowed so origin playback still works).
    """

    def _request(self, ip="203.0.113.50"):
        from starlette.requests import Request
        scope = {
            "type": "http", "method": "POST", "path": "/api/live/heartbeat",
            "headers": [(b"x-forwarded-for", ip.encode())],
            "query_string": b"", "client": (ip, 44444),
        }
        return Request(scope)

    def _set_limit(self, monkeypatch, value):
        """Pin the limiter budget regardless of any local .env override (live
        settings are read from os.environ on every call, and a dev .env with
        LIVE_TOKEN_RATE_LIMIT raised for load tests would otherwise leak in)."""
        monkeypatch.setenv("LIVE_TOKEN_RATE_LIMIT", str(value))

    def test_default_headroom_covers_10_shared_ip_viewers(self, monkeypatch):
        """10 viewers x 24 calls/min on ONE IP for one channel = 240 — the new
        default must absorb the full minute without tripping; only beyond it."""
        import pytest as _pytest
        from fastapi import HTTPException

        from app.routes import live

        self._set_limit(monkeypatch, 240)
        monkeypatch.setattr(live, "get_redis", AsyncMock(return_value=None))
        saved = dict(live._token_requests)
        live._token_requests.clear()
        try:
            req = self._request()
            for _ in range(240):
                # Must NOT raise for the full realistic shared-IP minute
                asyncio.run(live._check_viewer_rate_limit(req, "sunday-service"))
            # The 241st call inside the window trips the cap
            with _pytest.raises(HTTPException) as ei:
                asyncio.run(live._check_viewer_rate_limit(req, "sunday-service"))
            assert ei.value.status_code == 429
        finally:
            live._token_requests.clear()
            live._token_requests.update(saved)

    def test_budget_is_per_channel(self, monkeypatch):
        """A drained budget on channel A must never 429 channel B on the same IP
        (channel isolation extends to rate-limit state)."""
        from app.routes import live

        self._set_limit(monkeypatch, 240)
        monkeypatch.setattr(live, "get_redis", AsyncMock(return_value=None))
        saved = dict(live._token_requests)
        live._token_requests.clear()
        try:
            req = self._request()
            for _ in range(240):
                asyncio.run(live._check_viewer_rate_limit(req, "channel-a"))
            # Same IP, different channel: full fresh budget
            for _ in range(10):
                asyncio.run(live._check_viewer_rate_limit(req, "channel-b"))
        finally:
            live._token_requests.clear()
            live._token_requests.update(saved)

    def test_redis_path_trips_at_cap_and_keys_per_channel(self, monkeypatch):
        """Redis-backed limiter: 429 at the configured cap, and the counter key
        carries the channel slug (never a shared global bucket)."""
        from fastapi import HTTPException

        from app.routes import live

        store = {}
        redis = AsyncMock()

        async def fake_get(key):
            return str(store[key]) if key in store else None

        async def fake_incr(key):
            store[key] = store.get(key, 0) + 1
            return store[key]

        redis.get = fake_get
        redis.incr = fake_incr
        redis.expire = AsyncMock(return_value=True)
        monkeypatch.setattr(live, "get_redis", AsyncMock(return_value=redis))
        self._set_limit(monkeypatch, 240)
        self._set_limit(monkeypatch, 240)

        req = self._request()
        for _ in range(240):
            asyncio.run(live._check_viewer_rate_limit(req, "sunday-service"))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(live._check_viewer_rate_limit(req, "sunday-service"))
        assert ei.value.status_code == 429
        # Per-channel key shape: live_token_rate:{ip}:{channel}
        assert any("sunday-service" in k for k in store), store.keys()
        assert not any("other-channel" in k for k in store)


class TestOffHostPublishLifecycle:
    """Off-host MediaMTX topology (production): the backend cannot reach the
    control API (9997 stays internal), so the auth hook's publish_seen hint
    seeds the monitor's PUBLIC-playlist segment discovery, and segments
    registered from that playlist drive STARTING → LIVE."""

    def test_publish_seen_before_first_segment_is_starting(self):
        """Hook authorized a publish, segments not yet muxed → STARTING (the
        old code showed OFFLINE through the whole mux window in this topology)."""
        import asyncio
        import time as t
        assert asyncio.run(TestDerivedStatus()._derive(
            TestDerivedStatus()._row(),
            meta={"publish_seen": str(t.time() - 2)})) == "STARTING"

    def test_old_publish_seen_is_error_not_starting(self):
        """A publisher authorized but producing NO segments past the ERROR
        threshold is a failed ingest (encoder stalled / network blackhole /
        mux error) — it must surface ERROR, never a permanent STARTING and
        never a fake OFFLINE. A reconnecting encoder re-triggers the hook and
        refreshes publish_seen, so healthy retries stay STARTING."""
        import asyncio
        import time as t
        assert asyncio.run(TestDerivedStatus()._derive(
            TestDerivedStatus()._row(),
            meta={"publish_seen": str(t.time() - 300)})) == "ERROR"

    def test_register_segments_from_public_playlist(self):
        """Monitor registers segments by fetching the PUBLIC playlist —
        master → variant → seq extraction — with redirects followed and
        no-cache sent (stale edge cache must not mask a live stream)."""
        import asyncio

        import app.live_state as ls

        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2500000\nabc123_video1_stream.m3u8\n"
        variant = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
            "#EXT-X-MEDIA-SEQUENCE:41\n"
            "#EXTINF:2.0,\nabc123_video1_seg41.mp4\n"
            "#EXTINF:2.0,\nabc123_video1_seg42.mp4\n"
        )
        calls = []

        class FakeResp:
            def __init__(self, text):
                self.status_code = 200
                self._text = text
            @property
            def text(self):
                return self._text

        class FakeClient:
            def __init__(self, *a, **k):
                assert k.get("follow_redirects") is True, "must follow redirects"
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False
            async def get(self, url, headers=None):
                calls.append((url, headers))
                if url.endswith("index.m3u8"):
                    assert headers and headers.get("Cache-Control") == "no-cache"
                    return FakeResp(master)
                return FakeResp(variant)

        class FakeRedis:
            pass

        async def run():
            saved_redis = ls.get_redis
            saved_meta = ls.get_channel_meta
            saved_reg = ls.register_segment
            ls.get_redis = AsyncMock(return_value=FakeRedis())
            ls.get_channel_meta = AsyncMock(return_value={"latest_seq": "40"})
            reg = []

            async def fake_reg(slug, seq, duration=0.0, size=0):
                reg.append((slug, seq))
            ls.register_segment = fake_reg
            orig_client = ls.httpx.AsyncClient
            ls.httpx.AsyncClient = FakeClient
            try:
                await ls._register_segments_from_playlist("my-test")
            finally:
                ls.httpx.AsyncClient = orig_client
                ls.get_redis = saved_redis
                ls.get_channel_meta = saved_meta
                ls.register_segment = saved_reg
            return reg

        reg = asyncio.run(run())
        # Both seqs are newer than known latest_seq=40
        assert reg == [("my-test", 41), ("my-test", 42)], reg
        assert any("index.m3u8" in u for u, _ in calls)


class TestIngestDiagnostics:
    """GET /channels/{id}/ingest — the external-broadcaster debugging surface
    (Adam's OBS/Wirecast pilot): publisher state, auth outcome, HLS
    availability, reconnects, observed stream metadata. No secrets, no IPs."""

    def _channel(self, slug="adam", enabled=True):
        return {
            "id": uuid4(), "tenant_id": uuid4(), "owner_id": uuid4(),
            "name": "Adam", "slug": slug, "description": None,
            "stream_key_hash": "h", "stream_key_prefix": "live_ab…",
            "status": "OFFLINE", "enabled": enabled,
            "created_at": "2026-01-01", "updated_at": "2026-01-01",
            "last_seen_at": None,
        }

    def test_diagnostics_endpoint_reports_pipeline_stages(self, monkeypatch):
        import asyncio as aio
        import time as t

        from app.routes import live as live_routes

        row = self._channel()
        now = t.time()
        monkeypatch.setattr(live_routes, "require_channel_owner", AsyncMock(return_value=row))
        monkeypatch.setattr(live_routes, "get_channel_meta", AsyncMock(return_value={
            "publish_seen": str(now - 3),
            "last_seg_ts": str(now - 2),
            "latest_seq": "10",
            "ingest_bitrate_kbps": "4200",
            "codec": "H264", "resolution": "1280x720", "fps": "30.0",
        }))
        monkeypatch.setattr(live_routes, "latest_segment", AsyncMock(return_value=10))
        monkeypatch.setattr(live_routes, "resolve_ingress",
                            AsyncMock(return_value=(None, False)))
        monkeypatch.setattr(live_routes, "_probe_hls_playlist", AsyncMock(return_value=True))

        d = aio.run(live_routes.channel_ingest_diagnostics(row["id"], row["owner_id"]))
        assert d["status"] == "LIVE"
        assert d["publisher_connected"] is True
        assert d["hls_playlist_available"] is True
        assert d["ingest_bitrate_kbps"] == 4200
        assert d["codec"] == "H264" and d["resolution"] == "1280x720" and d["fps"] == 30.0
        assert d["auth_status"] in {"accepted", "denied", "unknown"}
        assert d["status_reason"]
        assert d["mediamtx_path"] == f"live/{row['slug']}"

    def test_diagnostics_error_stage_when_hls_missing(self, monkeypatch):
        """Publisher seen but no segments and no playlist → ERROR with an
        actionable reason (the stuck-STARTING failure mode, surfaced honestly)."""
        import asyncio as aio
        import time as t

        from app.routes import live as live_routes

        row = self._channel()
        monkeypatch.setattr(live_routes, "require_channel_owner", AsyncMock(return_value=row))
        monkeypatch.setattr(live_routes, "get_channel_meta", AsyncMock(return_value={
            "publish_seen": str(t.time() - 50),  # past the ERROR threshold
        }))
        monkeypatch.setattr(live_routes, "latest_segment", AsyncMock(return_value=None))
        monkeypatch.setattr(live_routes, "resolve_ingress",
                            AsyncMock(return_value=(None, False)))
        monkeypatch.setattr(live_routes, "_probe_hls_playlist", AsyncMock(return_value=False))

        d = aio.run(live_routes.channel_ingest_diagnostics(row["id"], row["owner_id"]))
        assert d["status"] == "ERROR"
        assert "not available" in d["status_reason"] or "failed" in d["status_reason"]

    def test_status_reason_strings(self):
        from app.routes import live as live_routes
        r = live_routes._ingest_stage_reason
        assert "waiting for MediaMTX" in r("STARTING", publisher_connected=True, hls_ok=False, seg_age=None, max_seg_age=12)
        assert "not advancing" in r("ERROR", publisher_connected=True, hls_ok=True, seg_age=60.0, max_seg_age=12)
        assert "No publisher" in r("OFFLINE", publisher_connected=False, hls_ok=None, seg_age=None, max_seg_age=12)
        assert "advancing" in r("LIVE", publisher_connected=True, hls_ok=True, seg_age=1.0, max_seg_age=12)


class TestPublishLifecycleTracking:
    """_track_publish_event / _track_disconnect / publish_lifecycle_snapshot —
    the auth-hook side state that powers diagnostics + reconnect counts.
    Secrets must never enter this state."""

    def test_first_seen_survives_reconnect(self):
        from app.routes import live_internal as li
        li._publish_lifecycle.clear()
        li._track_publish_event("adam", "ok")
        li._track_disconnect("adam")
        li._track_publish_event("adam", "ok")  # reconnect
        st = li.publish_lifecycle_snapshot("adam")
        assert st["publish_attempts"] == 2
        assert st["last_disconnect"] is not None
        # Uptime restarts after a disconnect (fresh publisher session), but
        # the attempt/reconnect history is preserved for diagnostics.
        first = li._publish_lifecycle["adam"]["publish_first_seen"]
        assert first > 0

    def test_denial_counting_and_outcomes(self):
        from app.routes import live_internal as li
        li._publish_lifecycle.clear()
        li._track_publish_event("eve", "denied")
        li._track_publish_event("eve", "denied")
        li._track_publish_event("eve", "challenge")
        st = li.publish_lifecycle_snapshot("eve")
        assert st["publish_attempts"] == 3
        assert st["publish_denied"] == 2
        assert st["last_publish_outcome"] == "challenge"
        assert st["publish_first_seen"] is None  # never accepted

    def test_snapshot_never_contains_secrets(self):
        from app.routes import live_internal as li
        li._publish_lifecycle.clear()
        li._track_publish_event("sunday", "ok")
        blob = str(li.publish_lifecycle_snapshot("sunday"))
        assert "pass=" not in blob and "password" not in blob


class TestSegmentIdentityEndpoint:
    """GET /segments/{slug}/{seq}/identity — the P2P leecher bootstrap.

    Playback-token gated (same contract as /session): the identity endpoint
    tells a viewer WHICH swarm to join for a segment; without auth it would be
    a free swarm-enumeration oracle for private channels.
    """

    def _request(self, ip="198.51.100.77", auth=None):
        from starlette.requests import Request
        headers = [(b"x-forwarded-for", ip.encode())]
        if auth:
            headers.append((b"authorization", auth.encode()))
        scope = {
            "type": "http", "method": "GET", "path": "/api/live/segments/ch/1/identity",
            "headers": headers, "query_string": b"", "client": (ip, 44444),
        }
        return Request(scope)

    def _row(self, channel_id):
        return {"id": channel_id, "slug": "ch", "name": "Ch", "enabled": True,
                "status": "LIVE", "tenant_id": channel_id, "owner_id": channel_id}

    def _auth(self, channel_id):
        """Save + restore limiter state and live settings touched by the call."""
        from app.routes import live
        saved = dict(live._token_requests)
        live._token_requests.clear()
        return saved

    @pytest.mark.asyncio
    async def test_requires_playback_token(self, monkeypatch):
        from uuid import uuid4

        from fastapi import HTTPException

        from app.routes import live
        cid = uuid4()
        pool = AsyncMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=self._row(cid))
        pool.acquire = lambda: AsyncMock(__aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock(return_value=False))
        monkeypatch.setattr(live, "get_db_pool", AsyncMock(return_value=pool))
        with pytest.raises(HTTPException) as ei:
            await live.segment_identity("ch", 3, self._request(auth="Bearer garbage.token"))
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_identity_for_valid_token(self, monkeypatch):
        from uuid import uuid4

        from app.routes import live
        cid = uuid4()
        pool = AsyncMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=self._row(cid))
        pool.acquire = lambda: AsyncMock(__aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock(return_value=False))
        monkeypatch.setattr(live, "get_db_pool", AsyncMock(return_value=pool))
        token = mint_playback_token(str(cid), ttl_seconds=60)
        ident = {"infohash": "a" * 40, "filename": "ch-3.m4s", "size": 1000}
        monkeypatch.setattr("app.live_p2p.get_segment_identity", AsyncMock(return_value=ident))
        got = await live.segment_identity("ch", 3, self._request(auth=f"Bearer {token}"))
        assert got == ident

    @pytest.mark.asyncio
    async def test_gone_segment_404s_never_stale(self, monkeypatch):
        from uuid import uuid4

        from fastapi import HTTPException

        from app.routes import live
        cid = uuid4()
        pool = AsyncMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=self._row(cid))
        pool.acquire = lambda: AsyncMock(__aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock(return_value=False))
        monkeypatch.setattr(live, "get_db_pool", AsyncMock(return_value=pool))
        token = mint_playback_token(str(cid), ttl_seconds=60)
        monkeypatch.setattr("app.live_p2p.get_segment_identity", AsyncMock(return_value=None))
        with pytest.raises(HTTPException) as ei:
            await live.segment_identity("ch", 999, self._request(auth=f"Bearer {token}"))
        assert ei.value.status_code == 404
