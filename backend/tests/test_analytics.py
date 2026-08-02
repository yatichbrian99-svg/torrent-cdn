"""
Unit tests for analytics routes: bandwidth-history (auth, 7-day zero-fill,
per-day GB aggregation), GET /analytics/{file_id} (auth + ownership), and
POST /analytics/event byte-counter validation.
"""
from datetime import datetime, timedelta
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
        expected_first = (datetime.utcnow().date() - timedelta(days=6)).isoformat()
        assert data[0]["date"] == expected_first
        assert data[-1]["date"] == datetime.utcnow().date().isoformat()
        for day in data:
            assert day["gb"] == 0

    def test_merges_query_results_with_zero_fill(self, client, auth_headers, mock_conn):
        """Should overlay real usage on the zero-filled window."""
        today = datetime.utcnow().date()
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
        """Absurd byte counters (>2GB per event) are rejected (422)."""
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 0,
            "bytes_origin": 3 * 1024**3,
        })
        assert resp.status_code == 422

    def test_valid_event_accepted(self, client, mock_conn):
        """A well-formed event with in-range counters is accepted."""
        mock_conn.fetchval.return_value = UUID(self.FILE_ID)  # file exists
        resp = client.post(self.URL, json={
            "file_id": self.FILE_ID,
            "event_type": "chunk_downloaded",
            "bytes_p2p": 100,
            "bytes_origin": 0,
        })
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Event tracked"
