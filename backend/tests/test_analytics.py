"""
Unit tests for analytics routes: bandwidth-history (auth, 7-day zero-fill,
per-day GB aggregation), GET /analytics/{file_id} (auth + ownership), and
POST /analytics/event byte-counter validation.
"""
from datetime import UTC, datetime, timedelta
from uuid import UUID


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
