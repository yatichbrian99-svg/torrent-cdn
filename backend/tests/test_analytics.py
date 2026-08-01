"""
Unit tests for analytics routes, focused on GET /analytics/bandwidth-history:
auth enforcement, 7-day zero-fill, and per-day GB aggregation.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock


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
