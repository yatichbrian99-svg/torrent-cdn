"""
Tests for the admin security + health dashboard (/admin/health) and the
bounded in-memory security-events recorder (app/security_events.py).
"""
from unittest.mock import AsyncMock

from app.security_events import record, recent, clear

ADMIN_URL = "/admin/health"


class TestAdminHealthAccess:
    def test_requires_admin(self, client, auth_headers, mock_conn):
        """A non-admin authed user must get 403 (same dependency as /admin/users)."""
        mock_conn.fetchrow.return_value = {"is_admin": False}
        resp = client.get(ADMIN_URL, headers=auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Admin access required"

    def test_no_token_gets_401(self, client):
        resp = client.get(ADMIN_URL)
        assert resp.status_code == 401

    def test_admin_gets_200_with_full_shape(self, client, admin_headers, mock_conn, mock_redis, mock_s3_client, monkeypatch):
        """Admin gets the full payload: checks, waf, posture, events, uptime."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 1  # SELECT 1
        mock_redis.ping = AsyncMock(return_value=True)
        # Storage head_bucket comes from mock_s3_client (already configured).
        # Hermetic: stub the WAF probe (it makes a real outbound fetch).
        import app.routes.admin as admin_mod
        monkeypatch.setattr(admin_mod._WafProbeCache, "get",
                            AsyncMock(return_value={"status": "unknown", "detail": "stubbed", "headers": {}}))

        resp = client.get(ADMIN_URL, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()

        # Infra checks present with a status per component.
        for key in ("database", "redis", "storage"):
            assert key in data["checks"]
            assert data["checks"][key]["status"] in ("ok", "absent", "error")

        # WAF probe is stubbed in tests.
        assert data["waf"]["status"] == "unknown"

        # Posture is a list of named checks with pass/warn statuses.
        assert isinstance(data["posture"], list)
        assert len(data["posture"]) >= 5
        for item in data["posture"]:
            assert "check" in item and "status" in item and "detail" in item
            assert item["status"] in ("pass", "warn")

        # Events feed is a list; uptime/generated_at are sane.
        assert isinstance(data["events"], list)
        assert isinstance(data["generated_at"], int)
        assert data["uptime_seconds"] >= 0


class TestSecurityEventsRecorder:
    def test_record_and_recent_newest_first(self):
        clear()
        record("login_failure", detail="a@x.com", ip="1.2.3.4")
        record("admin_toggle", detail="actor -> target")
        events = recent(10)
        assert len(events) == 2
        assert events[0]["type"] == "admin_toggle"   # newest first
        assert events[1]["type"] == "login_failure"
        assert events[1]["ip"] == "1.2.3.4"
        assert events[0]["detail"] == "actor -> target"
        clear()

    def test_bounded_feed(self):
        clear()
        for i in range(300):  # > MAX_EVENTS
            record("x", detail=str(i))
        events = recent(500)
        assert len(events) == 200  # hard cap, never unbounded
        assert events[0]["detail"] == "299"  # newest retained
        clear()

    def test_detail_truncated_and_safe(self):
        clear()
        record("x", detail="s" * 500)
        events = recent(1)
        assert len(events[0]["detail"]) == 160
        clear()

    def test_recent_limit_respected(self):
        clear()
        for i in range(10):
            record("x", detail=str(i))
        assert len(recent(3)) == 3
        assert len(recent(999)) == 10
        clear()
