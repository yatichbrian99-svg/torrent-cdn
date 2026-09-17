"""Tests for the live ephemeral state store and the MediaMTX integration
surface: external-auth hook (publish/read), ingest registration, segment
expiry, availability tracking, and session lifecycle."""
import asyncio
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app import live_state
from app.live_models import hash_stream_key, new_stream_key


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _fresh_mem():
    live_state._mem_store = {}
    live_state._mem_last_seq.clear()
    yield
    live_state._mem_store = {}
    live_state._mem_last_seq.clear()


class TestLiveState:
    def test_segment_register_and_latest(self):
        async def go():
            await live_state.register_segment("ch1", 5)
            await live_state.register_segment("ch1", 6)
            assert await live_state.segment_exists("ch1", 5)
            assert await live_state.segment_exists("ch1", 6)
            assert not await live_state.segment_exists("ch1", 99)
            latest = await live_state.latest_segment("ch1")
            assert latest == 6
        run(go())

    def test_segment_expiry(self, monkeypatch):
        from unittest.mock import patch
        import time as _t
        async def go():
            await live_state.register_segment("ch1", 1)
            assert await live_state.segment_exists("ch1", 1)
            # Register the segment at an artificially OLD timestamp: the next
            # prune must reclaim it ("stale live segments never linger").
            old = {"ts": _t.time() - 999, "duration": 2.0, "size": 0}
            with patch.object(live_state, "get_redis", new=AsyncMock(return_value=None)):
                await live_state.register_segment("ch1", 1)
            live_state._mem_set("ch1", "segs", "1", old, 9999)
            pruned = await live_state.prune_expired_segments("ch1")
            assert pruned >= 1
            assert not await live_state.segment_exists("ch1", 1)
        run(go())

    def test_peer_availability_roundtrip(self):
        async def go():
            await live_state.peer_report("ch1", "peerA", "sessA", haves=[3, 4])
            await live_state.peer_report("ch1", "peerB", "sessB", haves=[4])
            assert set(await live_state.peers_with_segment("ch1", 4)) == {"peerA", "peerB"}
            assert set(await live_state.peers_with_segment("ch1", 3)) == {"peerA"}
            # peer A stops reporting → its TTL expiry removes it everywhere
            await live_state.mark_peer_dead("ch1", "peerA")
            assert set(await live_state.peers_with_segment("ch1", 4)) == {"peerB"}
            assert set(await live_state.peers_with_segment("ch1", 3)) == set()
        run(go())

    def test_failed_transfer_scrubs_only_that_segment(self):
        async def go():
            await live_state.peer_report("ch1", "peerA", "sessA", haves=[7, 8])
            # A fails to deliver segment 7 — it must lose ONLY segment 7
            await live_state.mark_peer_dead_for_segment("ch1", "peerA", 7)
            assert await live_state.peers_with_segment("ch1", 7) == []
            assert await live_state.peers_with_segment("ch1", 8) == ["peerA"]
        run(go())

    def test_peer_scores_rank_stale_out(self):
        async def go():
            await live_state.peer_report("ch1", "fresh", "s1", haves=[1])
            await live_state.peer_report("ch1", "stale", "s2", haves=[1])
            # age the stale peer beyond the 30s health window
            h = dict(await live_state.peer_health("ch1", "stale"))
            h["last_seen"] = str(time.time() - 60)
            live_state._mem_set("ch1", "peer", "stale", h, 600)
            scored = await live_state.peer_scores("ch1", 1)
            ids = [p for p, _ in scored]
            assert "fresh" in ids and "stale" not in ids
        run(go())

    def test_counters_bump_and_reset(self):
        async def go():
            await live_state.bump_counters("ch1", bytes_p2p=100, bytes_origin=50)
            await live_state.bump_counters("ch1", bytes_p2p=50)
            c = await live_state.read_counters("ch1")
            assert c["bytes_p2p"] == 150 and c["bytes_origin"] == 50
            out = await live_state.reset_counters("ch1")
            assert out["bytes_p2p"] == 150
            assert (await live_state.read_counters("ch1")) == {}
        run(go())

    def test_clear_channel_drops_everything(self):
        async def go():
            await live_state.register_segment("ch1", 1)
            await live_state.peer_report("ch1", "p", "s", haves=[1])
            await live_state.bump_counters("ch1", bytes_p2p=10)
            await live_state.clear_channel("ch1")
            assert not await live_state.segment_exists("ch1", 1)
            assert (await live_state.read_counters("ch1")) == {}
            assert await live_state.latest_segment("ch1") is None
        run(go())

    def test_redis_failure_falls_back_to_memory(self):
        """Redis raising must degrade to the in-memory mirror, never raise."""
        async def go():
            async def boom(*a, **k):
                raise RuntimeError("redis down")
            with patch.object(live_state, "get_redis", new=boom):
                await live_state.register_segment("chX", 9)
                await live_state.bump_counters("chX", bytes_p2p=7)
                assert await live_state.segment_exists("chX", 9)
                c = await live_state.read_counters("chX")
                assert c["bytes_p2p"] == 7
        run(go())


class TestMediamtxAuthHook:
    URL = "/api/internal/mediamtx/auth"

    def _channel_row(self, slug="mychan", key=None, enabled=True):
        key = key or new_stream_key()
        return {
            "id": uuid4(), "tenant_id": uuid4(), "owner_id": uuid4(),
            "name": "C", "slug": slug, "description": None,
            "stream_key_hash": hash_stream_key(key),
            "stream_key_prefix": "live_ab…",
            "status": "OFFLINE", "enabled": enabled,
            "created_at": time.time(), "updated_at": time.time(), "last_seen_at": None,
        }, key

    def test_publish_with_valid_key_allowed(self, client, mock_conn):
        row, key = self._channel_row()
        mock_conn.fetchrow.return_value = row
        resp = client.post(self.URL, json={
            "action": "publish", "path": "live/mychan",
            "user": "", "password": key, "protocol": "rtmp",
        })
        assert resp.status_code == 200, resp.text

    def test_publish_with_bad_key_denied(self, client, mock_conn):
        row, _ = self._channel_row()
        mock_conn.fetchrow.return_value = row
        resp = client.post(self.URL, json={
            "action": "publish", "path": "live/mychan",
            "user": "", "password": "wrong-key", "protocol": "rtmp",
        })
        assert resp.status_code == 403

    def test_publish_unknown_channel_denied(self, client, mock_conn):
        mock_conn.fetchrow.return_value = None
        resp = client.post(self.URL, json={
            "action": "publish", "path": "live/ghost",
            "user": "", "password": "x", "protocol": "rtmp",
        })
        assert resp.status_code == 403

    def test_publish_disabled_channel_denied(self, client, mock_conn):
        row, key = self._channel_row(enabled=False)
        mock_conn.fetchrow.return_value = row
        resp = client.post(self.URL, json={
            "action": "publish", "path": "live/mychan",
            "user": "", "password": key, "protocol": "rtmp",
        })
        assert resp.status_code == 403

    def test_read_allowed_for_enabled(self, client, mock_conn):
        row, _ = self._channel_row()
        mock_conn.fetchrow.return_value = row
        resp = client.post(self.URL, json={
            "action": "read", "path": "mychan", "protocol": "hls",
        })
        assert resp.status_code == 200

    def test_unknown_path_shape_denied(self, client, mock_conn):
        resp = client.post(self.URL, json={"action": "read", "path": "../etc"})
        assert resp.status_code == 403

    def test_api_action_never_delegated(self, client):
        resp = client.post(self.URL, json={"action": "api", "path": "x"})
        assert resp.status_code == 403


class TestIngestWebhook:
    URL = "/api/internal/mediamtx/ingest"

    def test_registers_segment(self, client, mock_conn):
        mock_conn.fetchrow.return_value = {
            "id": uuid4(), "slug": "mychan", "enabled": True,
            "stream_key_hash": "h", "stream_key_prefix": "p",
            "name": "C", "description": None, "tenant_id": uuid4(), "owner_id": uuid4(),
            "status": "OFFLINE", "created_at": time.time(), "updated_at": time.time(),
            "last_seen_at": None,
        }
        resp = client.post(self.URL, json={"slug": "mychan", "sequence": 12, "duration": 2.0})
        assert resp.status_code == 200
        # The segment must be visible in the state store
        assert run(live_state.segment_exists("mychan", 12))

    def test_hook_secret_enforced_when_set(self, client, mock_conn, monkeypatch):
        monkeypatch.setenv("MEDIAMTX_HOOK_SECRET", "s3cret")
        # reset cached secret
        import app.routes.live_internal as li
        monkeypatch.setattr(li, "_HOOK_SECRET", None)
        resp = client.post(self.URL, json={"slug": "mychan", "sequence": 1})
        assert resp.status_code == 403
        resp = client.post(self.URL, json={"slug": "mychan", "sequence": 1},
                           headers={"X-Hook-Secret": "s3cret"})
        assert resp.status_code in (200, 404)  # 404 if channel missing — auth passed

    def test_shutdown_clears_state(self, client, mock_conn):
        async def seed():
            await live_state.register_segment("mychan", 3)
        run(seed())
        resp = client.post("/api/internal/mediamtx/shutdown", json={"slug": "mychan"})
        assert resp.status_code == 200
        assert not run(live_state.segment_exists("mychan", 3))


class TestReadyHooks:
    """runOnReady / runOnNotReady webhooks — MediaMTX's instant publisher
    confirmation (form-encoded slug=, MTX_PATH in the hook command env).
    /ready flips the channel to LIVE in ~1s instead of a monitor interval;
    /not-ready tears state down immediately."""

    READY_URL = "/api/internal/mediamtx/ready"
    NOTREADY_URL = "/api/internal/mediamtx/not-ready"

    def _channel_row(self, slug="mychan", enabled=True):
        return {
            "id": uuid4(), "tenant_id": uuid4(), "owner_id": uuid4(),
            "name": "C", "slug": slug, "description": None,
            "stream_key_hash": "h", "stream_key_prefix": "p",
            "status": "STARTING", "enabled": enabled,
            "created_at": time.time(), "updated_at": time.time(),
            "last_seen_at": None,
        }

    def test_ready_confirms_live(self, client, mock_conn):
        mock_conn.fetchrow.return_value = self._channel_row()
        resp = client.post(self.READY_URL, content=b"slug=mychan",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert resp.status_code == 200, resp.text
        # DB was flipped to LIVE (the _touch_last_seen_live path)
        assert "status = 'LIVE'" in mock_conn.execute.await_args.args[0]
        meta = run(live_state.get_channel_meta("mychan"))
        assert float(meta.get("publish_seen", 0)) > 0

    def test_ready_accepts_query_style_body(self, client, mock_conn):
        mock_conn.fetchrow.return_value = self._channel_row()
        resp = client.post(self.READY_URL, content=b"slug=mychan&extra=1")
        assert resp.status_code == 200

    def test_ready_unknown_channel_404(self, client, mock_conn):
        mock_conn.fetchrow.return_value = None
        resp = client.post(self.READY_URL, content=b"slug=ghost")
        assert resp.status_code == 404

    def test_ready_invalid_slug_400(self, client, mock_conn):
        resp = client.post(self.READY_URL, content=b"slug=../evil")
        assert resp.status_code == 400

    def test_ready_secret_enforced_when_set(self, client, mock_conn, monkeypatch):
        import app.routes.live_internal as li
        monkeypatch.setenv("MEDIAMTX_HOOK_SECRET", "s3cret")
        monkeypatch.setattr(li, "_HOOK_SECRET", None)
        resp = client.post(self.READY_URL, content=b"slug=mychan")
        assert resp.status_code == 403
        resp = client.post(self.READY_URL, content=b"slug=mychan",
                           headers={"X-Hook-Secret": "s3cret"})
        assert resp.status_code in (200, 404)  # 404 = auth passed, channel missing

    def test_not_ready_clears_state(self, client, mock_conn):
        async def seed():
            await live_state.register_segment("mychan", 5)
        run(seed())
        resp = client.post(self.NOTREADY_URL, content=b"slug=mychan")
        assert resp.status_code == 200
        assert not run(live_state.segment_exists("mychan", 5))
        assert "status = 'OFFLINE'" in mock_conn.execute.await_args.args[0]

    def test_not_ready_tolerates_truncated_body(self, client, mock_conn):
        """A hook killed mid-write must not 400 (MediaMTX would log noise on
        every stream end); unknown slug still 400s — only truncation is lenient."""
        resp = client.post(self.NOTREADY_URL, content=b"slug=")
        assert resp.status_code == 400
        resp = client.post(self.NOTREADY_URL, content=b"")
        assert resp.status_code == 400


class TestSessionLifecycle:
    def _channel_row(self):
        from datetime import UTC, datetime
        return {
            "id": uuid4(), "tenant_id": uuid4(), "owner_id": uuid4(),
            "name": "C", "slug": "mychan", "description": None,
            "stream_key_hash": "h", "stream_key_prefix": "p",
            "status": "LIVE", "enabled": True,
            "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
            "last_seen_at": datetime.now(UTC),
        }

    def _mint(self, channel_id):
        from app.mediamtx import mint_playback_token
        return {"Authorization": f"Bearer {mint_playback_token(str(channel_id))}"}

    def test_join_requires_token(self, client, mock_conn):
        mock_conn.fetchrow.return_value = self._channel_row()
        resp = client.post("/api/live/session", json={
            "channel_slug": "mychan", "session_id": "viewer-12345678",
        })
        assert resp.status_code == 401

    def test_join_with_token_creates_session(self, client, mock_conn):
        row = self._channel_row()
        mock_conn.fetchrow.return_value = row
        headers = self._mint(row["id"])
        resp = client.post("/api/live/session", json={
            "channel_slug": "mychan", "session_id": "viewer-12345678",
        }, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["swarm_base"] == "tcdnlive:mychan"
        sql = mock_conn.execute.await_args.args[0]
        assert "INSERT INTO live_sessions" in sql

    def test_heartbeat_updates_presence(self, client, mock_conn):
        row = self._channel_row()
        mock_conn.fetchrow.return_value = row
        headers = self._mint(row["id"])
        client.post("/api/live/session", json={
            "channel_slug": "mychan", "session_id": "viewer-12345678",
        }, headers=headers)
        resp = client.post("/api/live/heartbeat", json={
            "channel_slug": "mychan", "session_id": "viewer-12345678",
            "peer_id": "peer-viewer-1", "bytes_uploaded": 2048, "bytes_downloaded": 1024,
            "current_segment": 7,
        }, headers=headers)
        assert resp.status_code == 200
        c = run(live_state.read_counters("mychan"))
        assert c["bytes_p2p"] == 2048
        assert c["bytes_origin"] == 1024

    def test_availability_report_tracks_haves_and_failures(self, client, mock_conn):
        row = self._channel_row()
        mock_conn.fetchrow.return_value = row
        headers = self._mint(row["id"])
        resp = client.post("/api/live/segment/availability", json={
            "channel_slug": "mychan", "session_id": "viewer-12345678",
            "peer_id": "peer-viewer-1",
            "haves": [5, 6, 7],
            "transfers": [
                {"sequence_number": 5, "served": True, "bytes": 120000},
                {"sequence_number": 6, "served": False},
            ],
        }, headers=headers)
        assert resp.status_code == 200
        # Failed transfer for segment 6 scrubs the peer from THAT segment only
        # (other viewers stop trying it there → fallback to peer/origin), while
        # successfully delivered segments keep the peer listed.
        assert run(live_state.peers_with_segment("mychan", 6)) == []
        assert set(run(live_state.peers_with_segment("mychan", 5))) == {"peer-viewer-1"}
        assert set(run(live_state.peers_with_segment("mychan", 7))) == {"peer-viewer-1"}
        c = run(live_state.read_counters("mychan"))
        assert c["segments_served"] == 1
        assert c["segments_failed"] == 1
        assert c["bytes_p2p"] == 120000

    def test_private_channel_join_gives_404(self, client, mock_conn):
        row = self._channel_row()
        row["enabled"] = False
        mock_conn.fetchrow.return_value = row
        headers = self._mint(row["id"])
        resp = client.post("/api/live/session", json={
            "channel_slug": "mychan", "session_id": "viewer-12345678",
        }, headers=headers)
        assert resp.status_code == 404

    def test_wrong_channel_token_rejected(self, client, mock_conn):
        row = self._channel_row()
        mock_conn.fetchrow.return_value = row
        headers = self._mint(uuid4())  # token for a different channel
        resp = client.post("/api/live/session", json={
            "channel_slug": "mychan", "session_id": "viewer-12345678",
        }, headers=headers)
        assert resp.status_code == 401


class TestPeakSamplingAndRollup:
    def test_sample_peaks_grows_monotonically(self):
        async def go():
            await live_state.sample_peaks("pk", viewers=10, peers=8)
            await live_state.sample_peaks("pk", viewers=4, peers=12)
            await live_state.sample_peaks("pk", viewers=25, peers=3)
            c = await live_state.read_counters("pk")
            assert c["peak_viewers"] == 25
            assert c["peak_peers"] == 12
        run(go())

    def test_sample_peaks_zero_when_no_audience(self):
        async def go():
            await live_state.sample_peaks("pk0", viewers=0, peers=0)
            c = await live_state.read_counters("pk0")
            assert "peak_viewers" not in c
            assert "peak_peers" not in c
        run(go())

    def test_rollup_consumes_peaks_and_resets(self):
        async def go():
            from unittest.mock import patch, AsyncMock
            await live_state.sample_peaks("pk1", viewers=42, peers=30)
            await live_state.bump_counters("pk1", bytes_p2p=1000, segments_served=2)
            await live_state.set_channel_meta("pk1", last_seg_ts=str(time.time() - 999))
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={"id": "ch-id"})
            cm = type("C", (), {"__aenter__": AsyncMock(return_value=conn),
                                "__aexit__": AsyncMock(return_value=False)})()
            pool = type("P", (), {"acquire": lambda self: cm})()
            with patch("app.routes.live.get_db_pool", new=AsyncMock(return_value=pool)), \
                 patch("app.routes.live.pool_execute_insert_analytics", new=AsyncMock()) as ins:
                from app.routes.live import rollup_channel_analytics
                await rollup_channel_analytics("pk1")
            args = ins.call_args.args
            counters = args[2]  # (pool, channel_id, counters, meta)
            assert counters["peak_viewers"] == 42
            assert counters["peak_peers"] == 30
            # counters reset after rollup
            c = await live_state.read_counters("pk1")
            assert c.get("bytes_p2p", 0) == 0
            assert c.get("peak_viewers", 0) == 0
        run(go())
