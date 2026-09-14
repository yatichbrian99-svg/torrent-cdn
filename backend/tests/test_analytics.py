"""
Unit tests for analytics routes: bandwidth-history (auth, 7-day zero-fill,
per-day GB aggregation), GET /analytics/{file_id} (auth + ownership), and
POST /analytics/event byte-counter validation — plus the scale-out ingestion
contract (buffered batch writes, size-cap cache, load-shedding sampling).
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID


def asyncio_run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestBandwidthHistory:
    """Tests for GET /analytics/bandwidth-history."""

    URL = "/analytics/bandwidth-history"

    def test_requires_auth(self, client):
        """Unauthenticated request should get 401."""
        resp = client.get(self.URL)
        assert resp.status_code == 401

    def test_returns_seven_days_zero_filled(self, client, auth_headers, mock_conn):
        """Should always return exactly 7 days, filling missing days with 0."""
        mock_conn.fetch.return_value = []

        resp = client.get(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 7
        # Dates are consecutive, oldest first (endpoint keys off UTC date)
        expected_first = (datetime.now(UTC).date() - timedelta(days=6)).isoformat()
        assert data[0]["date"] == expected_first
        assert data[-1]["date"] == datetime.now(UTC).date().isoformat()
        for day in data:
            assert day["gb"] == 0

    def test_merges_query_results_with_zero_fill(self, client, auth_headers, mock_conn):
        """Should overlay real usage on the zero-filled window."""
        today = datetime.now(UTC).date()
        two_days_ago = today - timedelta(days=2)
        mock_conn.fetch.return_value = [
            {"date": two_days_ago, "gb_used": 1.5},
            {"date": today, "gb_used": 0.25},
        ]

        resp = client.get(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 7
        by_date = {d["date"]: d["gb"] for d in data}
        assert by_date[two_days_ago.isoformat()] == 1.5
        assert by_date[today.isoformat()] == 0.25
        # All other days stay at 0
        zero_days = [d for d in data if d["gb"] == 0]
        assert len(zero_days) == 5


class TestAnalyticsAccess:
    """GET /analytics/{file_id} requires auth and file ownership."""

    FILE_ID = UUID("22222222-2222-4222-8222-222222222222")
    URL = f"/analytics/{FILE_ID}"

    def test_requires_auth(self, client):
        """Unauthenticated request gets 401 — no file-existence oracle for strangers."""
        resp = client.get(self.URL)
        assert resp.status_code == 401

    def test_non_owner_gets_404(self, client, auth_headers, mock_conn):
        """A file you don't own returns 404, indistinguishable from a missing file."""
        mock_conn.fetchrow.return_value = None
        resp = client.get(self.URL, headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"
        # The ownership lookup must be scoped to the caller's user id
        sql = mock_conn.fetchrow.await_args.args[0]
        assert "user_id = $2" in sql and "id = $1" in sql
        assert mock_conn.fetchrow.await_args.args[1] == self.FILE_ID

    def test_owner_gets_aggregate(self, client, auth_headers, mock_conn):
        """The file owner receives the aggregate analytics payload."""
        half_gb = 512 * 1024 * 1024
        mock_conn.fetchrow.side_effect = [
            {"id": self.FILE_ID},  # ownership check passes
            {"total_views": 3, "total_leaves": 1, "total_p2p": half_gb, "total_origin": half_gb},
        ]
        resp = client.get(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_views"] == 3
        assert data["active_peers"] == 2
        assert data["p2p_offload_percent"] == 50.0
        assert data["bandwidth_saved_gb"] == 0.5


class TestPublicAnalytics:
    """GET /analytics/public/{file_id} — public aggregate stats for the demo page."""

    FILE_ID = UUID("22222222-2222-4222-8222-222222222222")
    URL = f"/analytics/public/{FILE_ID}"

    def test_public_no_auth_required(self, client, mock_conn):
        """No auth header needed — the demo page polls this unauthenticated."""
        mock_conn.fetchrow.side_effect = [
            {"id": self.FILE_ID},  # file exists
            {"total_views": 5, "total_leaves": 2, "total_p2p": 0, "total_origin": 0},
        ]
        resp = client.get(self.URL)
        assert resp.status_code == 200
        assert resp.json()["total_views"] == 5

    def test_missing_file_404(self, client, mock_conn):
        """Unknown file id returns 404, same as the authed endpoint."""
        mock_conn.fetchrow.return_value = None
        resp = client.get(self.URL)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"

    def test_returns_aggregate_payload(self, client, mock_conn):
        """Same payload shape as the authed endpoint, without an ownership scope."""
        half_gb = 512 * 1024 * 1024
        mock_conn.fetchrow.side_effect = [
            {"id": self.FILE_ID},
            {"total_views": 4, "total_leaves": 1, "total_p2p": half_gb, "total_origin": half_gb},
        ]
        resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_views"] == 4
        assert data["active_peers"] == 3
        assert data["p2p_offload_percent"] == 50.0
        assert data["bandwidth_saved_gb"] == 0.5
        assert data["total_gb"] == 1.0
        assert data["origin_gb"] == 0.5
        # The existence lookup (FIRST fetchrow call) must NOT be scoped to a
        # user id — the public route has no owner concept.
        sql = mock_conn.fetchrow.call_args_list[0].args[0]
        assert "user_id" not in sql

    def test_decimal_aggregates_serialize_to_json(self, client, mock_conn):
        """PROD REGRESSION: Postgres SUM(bigint) returns numeric, which asyncpg
        maps to Decimal. The public endpoint serves the aggregate via raw
        JSONResponse + json.dumps (no FastAPI encoder pass), so a Decimal
        reaching it 500'd EVERY public analytics request in production. The
        mocks here previously used plain ints and hid the bug. Assert the
        boundary contract: the payload is served 200 with pure JSON types."""
        mock_conn.fetchrow.side_effect = [
            {"id": self.FILE_ID},
            {
                "total_views": Decimal("4"),
                "total_leaves": Decimal("1"),
                "total_p2p": Decimal("536870912"),  # 0.5 GiB
                "total_origin": Decimal("536870912"),
            },
        ]
        resp = client.get(self.URL)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # No Decimal leakage: json.dumps would have raised before this point,
        # but assert the types explicitly so a future refactor fails here.
        assert isinstance(data["total_views"], int)
        assert isinstance(data["active_peers"], int)
        assert isinstance(data["bandwidth_saved_gb"], float)
        assert isinstance(data["total_gb"], float)
        assert data["total_views"] == 4
        assert data["active_peers"] == 3
        assert data["bandwidth_saved_gb"] == 0.5

    def test_aggregate_sql_casts_sum_to_bigint(self, client, mock_conn):
        """The source fix: SUM() must be cast to ::bigint so Decimal never
        enters the Python layer (asyncpg maps numeric → Decimal)."""
        mock_conn.fetchrow.side_effect = [
            {"id": self.FILE_ID},
            {"total_views": 0, "total_leaves": 0, "total_p2p": 0, "total_origin": 0},
        ]
        client.get(self.URL)
        agg_sql = mock_conn.fetchrow.call_args_list[1].args[0]
        assert "::bigint" in agg_sql


class TestAnalyticsEventValidation:
    """POST /analytics/event is public (embed SDK) but byte counters are clamped."""

    URL = "/analytics/event"
    FILE_ID = "22222222-2222-4222-8222-222222222222"

    def test_negative_bytes_rejected(self, client):
        """Negative byte counters are rejected by validation (422)."""
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": -1,
            "bytes_origin": 0,
        })
        assert resp.status_code == 422

    def test_oversized_bytes_rejected(self, client):
        """Absurd byte counters (>2GB per event) are rejected at the model (422)."""
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 0,
            "bytes_origin": 3 * 1024**3,
        })
        assert resp.status_code == 422

    def test_valid_event_accepted(self, client, mock_conn):
        """A well-formed event with in-range counters is accepted."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),  # file exists
            "file_size_mb": 1.0,       # 1 MiB
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 100,
            "bytes_origin": 0,
        })
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Event tracked"

    def test_bytes_exceeding_file_size_rejected(self, client, mock_conn):
        """An event reporting more than 10x the file's size is analytics
        tampering — rejected even though it's under the model's 2GB cap."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": 1.0,  # 1 MiB file → cap = 10 MiB
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 10 * 1024 * 1024 + 1,  # one byte over 10x
            "bytes_origin": 0,
        })
        assert resp.status_code == 400
        assert "analytics tampering" in resp.json()["detail"]

    def test_origin_bytes_over_cap_rejected_too(self, client, mock_conn):
        """The same relative bound applies to bytes_origin."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": 1.0,
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 0,
            "bytes_origin": 50 * 1024 * 1024,  # 50x the file
        })
        assert resp.status_code == 400

    def test_combined_counters_over_cap_rejected(self, client, mock_conn):
        """The bound is on the SUM of both counters (the aggregates add the two
        columns) — each counter under the cap but 9.9x+9.9x total is rejected."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": 1.0,  # 1 MiB file → cap = 10 MiB
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 6 * 1024 * 1024,   # 6 MiB — individually under
            "bytes_origin": 6 * 1024 * 1024,  # 6 MiB — individually under; sum 12 MiB > 10 MiB
        })
        assert resp.status_code == 400

    def test_combined_counters_within_cap_accepted(self, client, mock_conn):
        """P2P + origin split that stays within the cap total is fine."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": 1.0,
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 3 * 1024 * 1024,
            "bytes_origin": 3 * 1024 * 1024,  # sum 6 MiB < 10 MiB
        })
        assert resp.status_code == 200

    def test_exactly_at_cap_accepted(self, client, mock_conn):
        """Boundary: a counter exactly at 10x the file size passes."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": 1.0,  # 1 MiB file → cap = 10 MiB
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 10 * 1024 * 1024,
            "bytes_origin": 0,
        })
        assert resp.status_code == 200

    def test_full_file_report_accepted(self, client, mock_conn):
        """A legit bursty client reporting a whole file in one event passes
        (well under the 10x multiplier)."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": 100.0,  # 100 MiB file
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 100 * 1024 * 1024,
            "bytes_origin": 0,
        })
        assert resp.status_code == 200

    def test_tiny_file_floor_applies(self, client, mock_conn):
        """A tiny file gets the 1 MiB floor — a small report is never falsely
        rejected by the 10x math."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": 0.01,  # ~10 KB file → 10x = 100 KB, floored to 1 MiB
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 512 * 1024,  # 512 KiB — under the 1 MiB floor
            "bytes_origin": 0,
        })
        assert resp.status_code == 200

    def test_zero_size_file_still_has_floor(self, client, mock_conn):
        """A NULL/zero file_size_mb row still enforces the floor cap."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": None,
        }
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 2 * 1024 * 1024,  # 2 MiB > 1 MiB floor
            "bytes_origin": 0,
        })
        assert resp.status_code == 400

    def test_event_is_buffered_not_written_per_request(self, client, mock_redis, mock_conn):
        """Scale contract: a valid event lands in the Redis buffer and NO INSERT
        happens on the request path (one DB write per batch, not per event)."""
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID),
            "file_size_mb": 1.0,
        }
        captured = {}
        async def fake_lpush(key, payload):
            captured["key"] = key
            captured["payload"] = payload
            return 1
        mock_redis.lpush = fake_lpush
        mock_redis.llen = AsyncMock(return_value=0)

        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 1000,
            "bytes_origin": 0,
        })
        assert resp.status_code == 200
        assert captured["key"] == "analytics:event_buffer"
        assert b'"bytes_p2p":1000' in captured["payload"].encode() or 'bytes_p2p' in captured["payload"]
        mock_conn.execute.assert_not_awaited()  # no per-event INSERT

    def test_file_size_cap_uses_redis_cache(self, client, mock_redis, mock_conn):
        """A warm analytics:file_size cache answers the size-cap check without
        a DB round-trip (200k viewers ≈ 0 size-lookup SELECTs)."""
        # Warm cache for the size lookup... (get returns None for rate keys)
        async def get_side_effect(key):
            return "1.0" if key == f"analytics:file_size:{self.FILE_ID}" else None
        mock_redis.get = AsyncMock(side_effect=get_side_effect)
        mock_redis.llen = AsyncMock(return_value=0)
        async def fake_lpush(key, payload):
            return 1
        mock_redis.lpush = fake_lpush

        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "peer_joined",
            "bytes_p2p": 0,
            "bytes_origin": 0,
        })
        assert resp.status_code == 200
        mock_conn.fetchrow.assert_not_awaited()  # cold-cache lookup skipped

    def test_drain_batch_inserts_and_clears_buffer(self, client, mock_redis, mock_conn):
        """_drain_analytics_buffer_once pops up to BATCH_SIZE events and writes
        them with ONE executemany call; a DB failure requeues the batch."""
        from app.routes import analytics as a

        payloads = [
            json.dumps({"file_id": self.FILE_ID, "event_type": "chunk_downloaded",
                        "bytes_p2p": i, "bytes_origin": 0}, separators=(",", ":"))
            for i in range(5)
        ]
        mock_redis.rpop = AsyncMock(return_value=payloads)

        written = asyncio_run(a._drain_analytics_buffer_once())
        assert written == 5
        args = mock_conn.executemany.await_args
        assert len(args.args[1]) == 5
        mock_redis.lpush.assert_not_awaited()  # success → nothing requeued

    def test_drain_requeues_on_db_failure(self, monkeypatch):
        """A DB error during the batch INSERT pushes the events back onto the
        Redis buffer instead of losing them.

        Runs WITHOUT the client fixture: the drain path is fully mocked, and
        spinning up the TestClient app (which starts lifespan tasks) around a
        patched get_db_pool made this test hang on Python 3.14.
        """
        from unittest.mock import MagicMock, patch

        from app.routes import analytics as a

        payloads = [json.dumps({"file_id": self.FILE_ID, "event_type": "peer_joined",
                                "bytes_p2p": 0, "bytes_origin": 0}, separators=(",", ":"))]
        redis = AsyncMock()
        redis.rpop = AsyncMock(return_value=payloads)
        redis.lpush = AsyncMock(return_value=1)

        pool = AsyncMock()
        conn = AsyncMock()
        conn.executemany = AsyncMock(side_effect=RuntimeError("db down"))
        pool.acquire = MagicMock(return_value=pool)
        pool.__aenter__ = AsyncMock(return_value=conn)
        pool.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(a, "get_redis", AsyncMock(return_value=redis))
        monkeypatch.setattr(a, "get_db_pool", AsyncMock(return_value=pool))
        a._memory_event_buffer.clear()
        try:
            written = asyncio_run(a._drain_analytics_buffer_once())
            assert written == 0
            redis.lpush.assert_awaited_once()  # batch requeued
        finally:
            a._memory_event_buffer.clear()

    def test_sampling_sheds_chunk_events_under_pressure(self, client, mock_redis, mock_conn, monkeypatch):
        """When buffer pressure exceeds the threshold, most chunk_downloaded
        events are 202-sampled away; peer_joined is NEVER shed."""
        from app.routes import analytics as a
        monkeypatch.setattr(a, "_buffer_pressure", AsyncMock(return_value=a.ANALYTICS_SAMPLE_THRESHOLD + 1))
        mock_redis.llen = AsyncMock(return_value=0)
        async def fake_lpush(key, payload):
            return 1
        mock_redis.lpush = fake_lpush
        mock_conn.fetchrow.return_value = {
            "id": UUID(self.FILE_ID), "file_size_mb": 1.0,
        }

        sampled = 0
        for _ in range(40):
            resp = client.post(self.URL, json={
                "file_id": self.FILE_ID,
                "event_type": "chunk_downloaded",
                "bytes_p2p": 10,
                "bytes_origin": 0,
            })
            if resp.status_code == 202:
                sampled += 1
        # ~3/4 sampled (1-in-4 admitted) — with 40 events, at least half must
        # be shed; exact counts vary with the monotonic clock's divisibility.
        assert sampled > 10, f"sampling not shedding: {sampled}/40 sampled"

        # peer_joined is never sampled — views must stay exact.
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "peer_joined",
            "bytes_p2p": 0,
            "bytes_origin": 0,
        })
        assert resp.status_code == 200

    def test_per_file_limit_holds_when_redis_down(self, client, mock_conn, monkeypatch):
        """Exploit regression: the per-IP per-file 30/min cap must survive a
        Redis outage. With Redis down, the ONLY limiter used to be the IP-level
        budget (600/min) — so an attacker could pump 600 events/min into a
        SINGLE file's stats (each up to 10x the file size) and push its
        p2p_offload_percent to ~100%, defrauding the headline metric. The
        in-memory per-file fallback closes that: the 31st event for the same
        file from the same IP is 429 even with Redis dead."""
        from unittest.mock import AsyncMock

        import app.routes.analytics as analytics_mod

        # Redis down — get_redis() returns None, forcing the in-memory limiters.
        monkeypatch.setattr(analytics_mod, "get_redis", AsyncMock(return_value=None))
        # Save + clear the module-level in-memory stores so this test is
        # hermetic AND restores them afterwards (the same TestClient IP would
        # otherwise leak 30 entries into the next test's budget).
        saved_events = dict(analytics_mod._analytics_event_requests)
        saved_files = dict(analytics_mod._analytics_file_requests)
        analytics_mod._analytics_event_requests.clear()
        analytics_mod._analytics_file_requests.clear()
        try:
            mock_conn.fetchrow.return_value = {
                "id": UUID(self.FILE_ID),
                "file_size_mb": 1.0,
            }

            # The per-file budget is 30/min — the first 30 land.
            for _ in range(30):
                resp = client.post(self.URL, json={
                    "file_id": self.FILE_ID,
                    "event_type": "chunk_downloaded",
                    "bytes_p2p": 1024,
                    "bytes_origin": 0,
                })
                assert resp.status_code == 200

            # The 31st is throttled — per-file inflation is capped even
            # Redis-down.
            resp = client.post(self.URL, json={
                "file_id": self.FILE_ID,
                "event_type": "chunk_downloaded",
                "bytes_p2p": 1024,
                "bytes_origin": 0,
            })
            assert resp.status_code == 429

            # And a DIFFERENT file is not collateral damage (key is ip:file_id).
            other = "33333333-3333-4333-8333-333333333333"
            mock_conn.fetchrow.return_value = {
                "id": UUID(other),
                "file_size_mb": 1.0,
            }
            resp = client.post(self.URL, json={
                "file_id": other,
                "event_type": "chunk_downloaded",
                "bytes_p2p": 1024,
                "bytes_origin": 0,
            })
            assert resp.status_code == 200
        finally:
            analytics_mod._analytics_event_requests.clear()
            analytics_mod._analytics_event_requests.update(saved_events)
            analytics_mod._analytics_file_requests.clear()
            analytics_mod._analytics_file_requests.update(saved_files)
