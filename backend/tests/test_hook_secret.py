"""The MediaMTX hook secret: constant-time, header-first, query deprecated.

Two problems in the old check:

1. ``presented == secret`` is a byte-by-byte comparison that returns at the
   first difference — a timing oracle against a shared secret that guards the
   stream-key auth hook.
2. ``?secret=`` was accepted as an equal alternative. A URL is copied into
   access logs, reverse-proxy logs, browser history and Referer headers, so the
   secret lands somewhere nobody intended — the same secret that authenticates
   every hook.

The header is now the interface (the runOnReady/runOnNotReady hooks already send
it). The query survives only as an explicit, warned, opt-in deprecation path,
because stock MediaMTX <= 1.11 has no way to add a header to authHTTPAddress; in
production it is off unless the operator sets ``MEDIAMTX_ALLOW_QUERY_SECRET=1``.

These tests call ``_check_hook_secret`` directly: through the route a rejected
secret and a rejected stream key both surface as 403, so the route response
cannot tell the two apart.
"""

from __future__ import annotations

import hmac

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.routes.live_internal as li

SECRET = "hook-secret-for-tests"
AUTH_PATH = "/api/internal/mediamtx/auth"


@pytest.fixture(autouse=True)
def hook(monkeypatch):
    monkeypatch.setenv("MEDIAMTX_HOOK_SECRET", SECRET)
    monkeypatch.delenv("MEDIAMTX_ALLOW_QUERY_SECRET", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(li, "_HOOK_SECRET", None)
    monkeypatch.setattr(li, "_HOOK_WARNED", False)
    monkeypatch.setattr(li, "_QUERY_SECRET_WARNED", False, raising=False)
    li._hook_bad_attempts.clear()
    yield
    monkeypatch.setattr(li, "_HOOK_SECRET", None)
    li._hook_bad_attempts.clear()


def _request(headers: dict[str, str] | None = None, query: str = "") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": AUTH_PATH,
            "raw_path": AUTH_PATH.encode(),
            "query_string": query.encode(),
            "headers": raw,
            "client": ("203.0.113.7", 4444),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _rejected(request: Request) -> bool:
    with pytest.raises(HTTPException) as caught:
        li._check_hook_secret(request)
    assert caught.value.status_code in (403, 429)
    return True


class TestHeader:
    def test_correct_header_is_accepted(self):
        assert li._check_hook_secret(_request({"X-Hook-Secret": SECRET})) is None

    def test_wrong_header_is_rejected(self):
        assert _rejected(_request({"X-Hook-Secret": SECRET + "x"}))

    def test_missing_secret_is_rejected(self):
        assert _rejected(_request())


class TestConstantTime:
    def test_the_comparison_goes_through_compare_digest(self, monkeypatch):
        """Pin the primitive, not just the verdict: `==` on str is the oracle."""
        seen: list[tuple[str, str]] = []
        real = hmac.compare_digest

        def spy(a, b):
            seen.append((a, b))
            return real(a, b)

        monkeypatch.setattr(li.hmac, "compare_digest", spy)
        li._check_hook_secret(_request({"X-Hook-Secret": SECRET}))
        assert seen == [(SECRET.encode(), SECRET.encode())]

    def test_a_non_ascii_header_does_not_raise(self):
        """compare_digest raises on non-ASCII str; a header can carry anything."""
        assert _rejected(_request({"X-Hook-Secret": "\u00e9\u00e9\u00e9"}))


class TestQueryIsDeprecated:
    def test_query_secret_is_refused_in_production_by_default(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        assert _rejected(_request(query=f"secret={SECRET}"))

    def test_query_secret_still_works_in_dev(self):
        """Docker compose's MediaMTX can only carry it in the URL."""
        assert li._check_hook_secret(_request(query=f"secret={SECRET}")) is None

    def test_the_deprecation_path_is_explicit_and_warned(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("MEDIAMTX_ALLOW_QUERY_SECRET", "1")
        with caplog.at_level(logging.WARNING, logger="app.routes.live_internal"):
            assert li._check_hook_secret(_request(query=f"secret={SECRET}")) is None
        assert any("deprecated" in r.message for r in caplog.records)

    def test_a_wrong_query_secret_is_rejected(self):
        assert _rejected(_request(query="secret=not-the-secret"))

    def test_the_header_wins_over_a_stale_query_secret(self):
        request = _request({"X-Hook-Secret": SECRET}, query="secret=stale")
        assert li._check_hook_secret(request) is None
