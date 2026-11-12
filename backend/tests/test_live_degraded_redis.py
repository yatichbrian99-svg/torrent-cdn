"""The viewer rate limiter must degrade, never 500, when a Redis command fails.

Evidence this is a real failure class (2026-10-30 ZAP run): one 500 each on
``/api/live/session``, ``/api/live/heartbeat`` and
``/api/live/segment/availability`` — all three in the same second, and all three
stopping inside the one statement the three handlers share,
``_check_viewer_rate_limit``. ``get_redis()`` only returns ``None`` when it can
*prove* Redis is down (its liveness PING fails); a client that PINGs fine and
then raises on the next command (OOM, WRONGTYPE, a timeout race, a recycle
between the two calls) used to raise straight out of the handler — even though
this function carries an in-memory fallback for exactly that situation.

These tests drive the HTTP routes with a Redis stub whose commands raise, which
is deterministic and does not depend on reproducing the original incident.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ResponseError

from app.routes import live as live_routes

#: Bodies shaped like the ones the player sends. Every route below calls the
#: rate limiter before it touches the database, so these reach the limiter.
BODIES = {
    "/api/live/session": {
        "channel_slug": "zap-channel",
        "session_id": "s" * 16,
    },
    "/api/live/heartbeat": {
        "channel_slug": "zap-channel",
        "session_id": "s" * 16,
        "peer_id": "p" * 16,
    },
    "/api/live/segment/availability": {
        "channel_slug": "zap-channel",
        "session_id": "s" * 16,
        "peer_id": "p" * 16,
        "haves": [1, 2],
        "transfers": [],
    },
}


class _RaisingRedis:
    """A client that PINGs fine (get_redis handed it out) but fails on use."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def get(self, *args, **kwargs):
        raise self._exc

    async def incr(self, *args, **kwargs):
        raise self._exc

    async def expire(self, *args, **kwargs):
        raise self._exc


def _empty_pool():
    """A pool whose channel lookup finds nothing: after the limiter the route
    answers 404, which is the pass-through we assert on."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


@pytest.fixture(autouse=True)
def _clean_fallback():
    live_routes._token_requests.clear()
    yield
    live_routes._token_requests.clear()


def _install(monkeypatch, *, limit: int = 240):
    async def _redis():
        return _RaisingRedis(
            ResponseError("OOM command not allowed when used memory > 'maxmemory'.")
        )

    async def _pool():
        return _empty_pool()

    monkeypatch.setattr(live_routes, "get_redis", _redis)
    monkeypatch.setattr(live_routes, "get_db_pool", _pool)
    monkeypatch.setattr(
        live_routes,
        "get_live_settings",
        lambda: SimpleNamespace(token_rate_limit=limit, hls_segment_duration=2),
    )


@pytest.mark.parametrize("path", sorted(BODIES))
def test_redis_command_error_is_never_a_500(client, monkeypatch, path):
    _install(monkeypatch)
    response = client.post(path, json=BODIES[path])
    assert response.status_code < 500, response.text
    # The request still reaches the channel lookup (404 = no such channel),
    # i.e. the limiter handed control back instead of aborting the request.
    assert response.status_code == 404, response.text


def test_fallback_still_enforces_the_budget(client, monkeypatch):
    """Degrading must not mean "stop limiting": with Redis failing, the
    in-memory window is what enforces the per-IP budget."""
    _install(monkeypatch, limit=2)
    codes = [
        client.post(
            "/api/live/heartbeat", json=BODIES["/api/live/heartbeat"]
        ).status_code
        for _ in range(3)
    ]
    assert codes[:2] == [404, 404], codes
    assert codes[2] == 429, codes
