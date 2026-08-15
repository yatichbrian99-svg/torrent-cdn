"""
Tests for the admin security + health dashboard (/admin/health) and the
bounded in-memory security-events recorder (app/security_events.py).
"""
from unittest.mock import AsyncMock

from app.security_events import clear, recent, record

ADMIN_URL = "/admin/health"
# /admin/health/status is the same handler registered under a second path — the
# frontend calls THIS one because the Next.js page at /admin/health shadows the
# Vercel rewrite (same-origin GET /admin/health would return HTML, not JSON).
ADMIN_URL_ALIAS = "/admin/health/status"


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

    def test_alias_status_route_serves_same_payload(self, client, admin_headers, mock_conn, mock_redis, mock_s3_client, monkeypatch):
        """The frontend calls /admin/health/status (the /admin/health page shadows
        the proxy rewrite) — the alias must serve the identical payload."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 1
        mock_redis.ping = AsyncMock(return_value=True)
        import app.routes.admin as admin_mod
        monkeypatch.setattr(admin_mod._WafProbeCache, "get",
                            AsyncMock(return_value={"status": "unknown", "detail": "stubbed", "headers": {}}))

        resp = client.get(ADMIN_URL_ALIAS, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        for key in ("database", "redis", "storage"):
            assert key in data["checks"]
        assert data["waf"]["status"] == "unknown"
        assert isinstance(data["events"], list)


class TestAdminHealthRedisStates:
    """The Redis check must distinguish 'not configured' (env var absent) from
    'unreachable' (set but connection failed) so the dashboard is actionable."""

    def _health(self, client, admin_headers, monkeypatch):
        import app.routes.admin as admin_mod
        monkeypatch.setattr(admin_mod._WafProbeCache, "get",
                            AsyncMock(return_value={"status": "unknown", "detail": "stubbed", "headers": {}}))
        resp = client.get(ADMIN_URL, headers=admin_headers)
        assert resp.status_code == 200
        return resp.json()

    def test_redis_unset_reports_not_configured(self, client, admin_headers, mock_conn, mock_redis, mock_s3_client, monkeypatch):
        """REDIS_URL absent -> 'absent / not configured', and Redis is never
        touched (nothing to connect to)."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 1
        monkeypatch.delenv("REDIS_URL", raising=False)

        data = self._health(client, admin_headers, monkeypatch)
        assert data["checks"]["redis"]["status"] == "absent"
        assert "not configured" in data["checks"]["redis"]["detail"]
        mock_redis.ping.assert_not_called()

    def test_redis_unreachable_reports_unreachable(self, client, admin_headers, mock_conn, mock_redis, mock_s3_client, monkeypatch):
        """REDIS_URL set but get_redis() returns None -> 'absent / unreachable'."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 1
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")
        # get_redis is imported INSIDE the health handler, so it resolves the
        # app.database binding at call time — patch that (like conftest does).
        import app.database as database_mod
        monkeypatch.setattr(database_mod, "get_redis", AsyncMock(return_value=None))
        # Simulate a recorded underlying failure — the detail must surface it so
        # the dashboard says WHY (DNS/TLS/auth/timeout), not just "unreachable".
        monkeypatch.setattr(database_mod, "last_redis_error", "TimeoutError: connect timed out")

        data = self._health(client, admin_headers, monkeypatch)
        assert data["checks"]["redis"]["status"] == "absent"
        assert "unreachable" in data["checks"]["redis"]["detail"]
        assert "TimeoutError: connect timed out" in data["checks"]["redis"]["detail"]

    def test_redis_ok_when_ping_succeeds(self, client, admin_headers, mock_conn, mock_redis, mock_s3_client, monkeypatch):
        """REDIS_URL set + ping ok -> 'ok'."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 1
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379")
        mock_redis.ping = AsyncMock(return_value=True)

        data = self._health(client, admin_headers, monkeypatch)
        assert data["checks"]["redis"]["status"] == "ok"
        assert data["checks"]["redis"]["detail"] == "ping ok"


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
