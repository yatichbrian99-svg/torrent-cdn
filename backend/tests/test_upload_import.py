"""
Tests for POST /upload/import (import-by-URL).

The endpoint makes the SERVER fetch a user-supplied URL, so the SSRF guard is
the security-critical surface and gets the bulk of the coverage:

  * _import_url_is_safe — literals (private/loopback/link-local/CGNAT) are
    blocked without DNS; hostnames are resolved and every address checked;
    localhost-ish suffixes are blocked outright.
  * _download_import_url — every redirect hop re-runs the guard, byte cap and
    time budget enforced, friendly HTTP errors.
  * _import_filename — explicit filename > URL basename > Content-Type map.
  * POST /upload/import e2e — rides the shared _store_and_finalize pipeline,
    so usage gating, rate limiting, malware scan, torrent build, and
    content-hash dedup all apply identically to imported files.
"""
from unittest.mock import MagicMock, AsyncMock, patch
from uuid import UUID

import httpx
import pytest


# ── Scripted httpx.AsyncClient stand-in (same pattern as test_malware_scan) ──
class _FakeImportResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {}

    async def aiter_bytes(self, chunk_size):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]


class _FakeImportClient:
    """Pops one scripted (status, content, headers) tuple per get() call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(url)
        status, content, headers = self.responses.pop(0)
        return _FakeImportResponse(status, content, headers)


def _patch_import_client(monkeypatch, responses):
    """Point the route's httpx.AsyncClient at a scripted fake; returns the fake."""
    fake = _FakeImportClient(responses)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


class TestImportUrlIsSafe:
    """The SSRF guard: URL shape + literal IPs + resolved hostname IPs."""

    def _safe(self, url, monkeypatch):
        from app.routes.upload import _import_url_is_safe
        return _import_url_is_safe(url)

    def test_rejects_non_http_schemes(self, monkeypatch):
        from app.routes.upload import _import_url_is_safe
        assert _import_url_is_safe("ftp://example.com/file.mp4") is False
        assert _import_url_is_safe("file:///etc/passwd") is False
        assert _import_url_is_safe("gopher://example.com") is False
        assert _import_url_is_safe("//example.com/file.mp4") is False
        assert _import_url_is_safe("") is False

    def test_blocks_private_loopback_linklocal_and_cgnat_literals(self, monkeypatch):
        from app.routes.upload import _import_url_is_safe
        for bad in (
            "http://127.0.0.1/x",
            "http://127.1.2.3/x",
            "http://10.0.0.5/x",
            "http://192.168.1.1/x",
            "http://172.16.0.1/x",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://0.0.0.0/x",
            "http://100.64.0.1/x",  # CGNAT
            "http://[::1]/x",
            "http://[fe80::1]/x",
            "http://[fc00::1]/x",
        ):
            assert _import_url_is_safe(bad) is False, bad

    def test_accepts_public_ip_literals(self, monkeypatch):
        from app.routes.upload import _import_url_is_safe
        assert _import_url_is_safe("http://8.8.8.8/file.mp4") is True
        assert _import_url_is_safe("http://93.184.216.34/video.mp4") is True

    def test_blocks_localhost_suffixes(self, monkeypatch):
        from app.routes.upload import _import_url_is_safe
        assert _import_url_is_safe("http://localhost/x") is False
        assert _import_url_is_safe("http://db.internal/x") is False
        assert _import_url_is_safe("http://router.local/x") is False
        assert _import_url_is_safe("http://nas.lan/x") is False

    def test_resolves_hostname_and_blocks_private_resolution(self, monkeypatch):
        """A public-looking hostname that RESOLVES to a private address is
        blocked (the classic metadata-SSRF via a friendly hostname)."""
        import socket
        import app.routes.upload as upload
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, port, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 0))
            ],
        )
        assert upload._import_url_is_safe("http://metadata.example.com/latest/") is False

    def test_resolves_hostname_and_accepts_public_resolution(self, monkeypatch):
        import socket
        import app.routes.upload as upload
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, port, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
            ],
        )
        assert upload._import_url_is_safe("http://example.com/video.mp4") is True

    def test_any_private_answer_rejects(self, monkeypatch):
        """A hostname resolving to BOTH public and private addresses is still
        rejected (one bad address poisons the whole check)."""
        import socket
        import app.routes.upload as upload
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, port, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 0)),
            ],
        )
        assert upload._import_url_is_safe("http://example.com/x") is False

    def test_dns_failure_rejects(self, monkeypatch):
        import socket
        import app.routes.upload as upload
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no such host")))
        assert upload._import_url_is_safe("http://nonexistent.invalid/x") is False


class TestImportPinnedAddress:
    """_import_pinned_address resolves + validates ONCE and returns the exact
    (host, ip) pair the downloader connects to — the DNS-rebinding fix."""

    def test_public_ip_literal(self):
        from app.routes.upload import _import_pinned_address
        assert _import_pinned_address("http://8.8.8.8/v.mp4") == ("8.8.8.8", "8.8.8.8")

    def test_private_literal_returns_none(self):
        from app.routes.upload import _import_pinned_address
        for url in (
            "http://127.0.0.1/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/x",
            "http://[::1]/x",
        ):
            assert _import_pinned_address(url) is None, url

    def test_bad_scheme_or_suffix_returns_none(self):
        from app.routes.upload import _import_pinned_address
        for url in ("ftp://example.com/x", "file:///etc/passwd", "http://db.internal/x", "http://localhost/x"):
            assert _import_pinned_address(url) is None, url

    def test_hostname_resolution_returns_validated_ip(self, monkeypatch):
        import socket
        from app.routes.upload import _import_pinned_address
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        )
        host, ip = _import_pinned_address("https://example.com/v.mp4")
        assert host == "example.com"
        assert ip == "93.184.216.34"

    def test_any_private_answer_returns_none(self, monkeypatch):
        import socket
        from app.routes.upload import _import_pinned_address
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, port, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 0)),
            ],
        )
        assert _import_pinned_address("http://example.com/x") is None

    def test_dns_failure_returns_none(self, monkeypatch):
        import socket
        from app.routes.upload import _import_pinned_address
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("nxdomain")))
        assert _import_pinned_address("http://nonexistent.invalid/x") is None


class TestPinnedNetworkBackend:
    """The httpcore network backend must connect ONLY to the pre-validated IP,
    never to the attacker-controlled hostname from the request URL."""

    def test_connect_tcp_targets_the_pinned_ip(self, monkeypatch):
        import asyncio
        import anyio
        from app.routes.upload import _PinnedNetworkBackend

        connected = []

        async def fake_connect_tcp(*args, **kwargs):
            connected.append(kwargs.get("remote_host"))
            return object()  # AnyIOStream wraps it lazily; socket_options is empty

        # anyio.fail_after is a SYNC context manager (`with fail_after(...)`),
        # even inside async code — mirror that.
        def fake_fail_after(*a, **k):
            class _CM:
                def __enter__(self): return None
                def __exit__(self, *x): return False
            return _CM()

        monkeypatch.setattr(anyio, "connect_tcp", fake_connect_tcp)
        monkeypatch.setattr(anyio, "fail_after", fake_fail_after)

        async def run():
            backend = _PinnedNetworkBackend("93.184.216.34")
            await backend.connect_tcp("evil.example.com", 443)

        asyncio.run(run())
        # The connect targeted the validated IP — the hostname was never used.
        assert connected == ["93.184.216.34"]


class TestImportFilename:
    """Filename derivation: explicit > URL basename > Content-Type map."""

    def _name(self, url, content_type="", user_filename=None):
        from app.routes.upload import _import_filename
        return _import_filename(user_filename, url, content_type)

    def test_explicit_filename_wins(self):
        # Explicit filenames still go through sanitization (spaces → _).
        assert self._name("https://example.com/x", "video/mp4", "my clip.mp4") == "my_clip.mp4"

    def test_explicit_filename_is_sanitized(self):
        assert self._name("https://example.com/x", "", "../../evil.mp4") == "evil.mp4"

    def test_url_basename_used_when_no_filename(self):
        assert self._name("https://example.com/videos/s01e01.mp4") == "s01e01.mp4"

    def test_url_basename_ignores_query_string(self):
        # A path with no extension can't be typed from the URL alone — the
        # filename must never leak query-string junk like `file=clip.webm`.
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            self._name("https://example.com/dl?file=clip.webm&token=abc")
        assert self._name("https://example.com/clip.mp4?token=abc") == "clip.mp4"

    def test_content_type_fallback(self):
        assert self._name("https://example.com/stream", "video/mp4") == "imported.mp4"
        assert self._name("https://example.com/stream", "video/webm") == "imported.webm"
        assert self._name("https://example.com/stream", "audio/mpeg") == "imported.mp3"

    def test_unsupported_raises_400(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            self._name("https://example.com/data.bin", "application/octet-stream")
        assert ei.value.status_code == 400

    def test_bad_extension_falls_through_to_content_type(self):
        # The URL basename has a bad extension, but the Content-Type is known.
        assert self._name("https://example.com/photo.jpg", "video/mp4") == "imported.mp4"


class TestDownloadImportUrl:
    """The size-capped, redirect-guarded downloader."""

    def test_download_writes_bytes_and_returns_metadata(self, monkeypatch, tmp_path):
        import asyncio
        from app.routes.upload import _download_import_url
        body = b"\x00\x00\x00\x20ftypmp42" + b"x" * 1024  # 12 + 1024 = 1036
        fake = _FakeImportClient([
            (200, body, {"content-type": "video/mp4; charset=utf-8", "content-length": str(len(body))}),
        ])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
        path = tmp_path / "dl.bin"

        content_type, total = asyncio.run(_download_import_url("https://example.com/a.mp4", str(path), 5 * 1024 * 1024))

        assert content_type == "video/mp4"  # charset stripped
        assert total == len(body)
        assert path.read_bytes() == body

    def test_declared_size_over_cap_is_rejected(self, monkeypatch, tmp_path):
        import asyncio
        from app.routes.upload import _download_import_url
        from fastapi import HTTPException
        fake = _FakeImportClient([
            (200, b"x", {"content-length": str(10 * 1024 * 1024)}),
        ])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)

        with pytest.raises(HTTPException) as ei:
            asyncio.run(_download_import_url(
                "https://example.com/big.mp4", str(tmp_path / "dl.bin"), 5 * 1024 * 1024
            ))
        assert ei.value.status_code == 413

    def test_streamed_size_over_cap_is_rejected(self, monkeypatch, tmp_path):
        """Even with no/lying Content-Length the byte cap holds during the stream."""
        import asyncio
        from app.routes.upload import _download_import_url
        from fastapi import HTTPException
        fake = _FakeImportClient([
            (200, b"y" * (6 * 1024 * 1024), {}),
        ])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)

        with pytest.raises(HTTPException) as ei:
            asyncio.run(_download_import_url(
                "https://example.com/big.mp4", str(tmp_path / "dl.bin"), 5 * 1024 * 1024
            ))
        assert ei.value.status_code == 413

    def test_redirect_followed_and_guard_rerun(self, monkeypatch, tmp_path):
        import asyncio
        import socket
        from app.routes.upload import _download_import_url
        # Deterministic DNS: every hostname resolves public (the sandbox's DNS
        # may resolve unknown hosts to a private block page, which the guard
        # correctly rejects — that's not what this test exercises).
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        )
        fake = _FakeImportClient([
            (302, b"", {"location": "https://cdn.example.com/final.mp4"}),
            (200, b"abc", {"content-type": "video/mp4"}),
        ])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)

        ct, total = asyncio.run(_download_import_url("https://example.com/r", str(tmp_path / "dl.bin"), 1024 * 1024))
        assert total == 3
        assert fake.calls == ["https://example.com/r", "https://cdn.example.com/final.mp4"]

    def test_redirect_to_private_is_blocked_before_fetch(self, monkeypatch, tmp_path):
        import asyncio
        from app.routes.upload import _download_import_url
        from fastapi import HTTPException
        fake = _FakeImportClient([
            (302, b"", {"location": "http://169.254.169.254/latest/meta-data/"}),
            (200, b"should never fetch", {}),
        ])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)

        with pytest.raises(HTTPException) as ei:
            asyncio.run(_download_import_url("https://example.com/r", str(tmp_path / "dl.bin"), 1024 * 1024))
        assert ei.value.status_code == 400
        assert len(fake.calls) == 1  # the private hop was never requested

    def test_non_200_raises_http_error(self, monkeypatch, tmp_path):
        import asyncio
        from app.routes.upload import _download_import_url
        from fastapi import HTTPException
        fake = _FakeImportClient([(404, b"nope", {})])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)

        with pytest.raises(HTTPException) as ei:
            asyncio.run(_download_import_url("https://example.com/missing", str(tmp_path / "dl.bin"), 1024 * 1024))
        assert ei.value.status_code == 400
        assert "404" in ei.value.detail

    def test_timeout_returns_504(self, monkeypatch, tmp_path):
        import asyncio
        from app.routes.upload import _download_import_url
        from fastapi import HTTPException

        class _TimeoutClient(_FakeImportClient):
            async def get(self, *a, **k):
                raise httpx.TimeoutException("timed out")

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _TimeoutClient([]))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(_download_import_url("https://example.com/slow", str(tmp_path / "dl.bin"), 1024 * 1024))
        assert ei.value.status_code == 504

    def test_connect_error_returns_502(self, monkeypatch, tmp_path):
        import asyncio
        from app.routes.upload import _download_import_url
        from fastapi import HTTPException

        class _ConnectClient(_FakeImportClient):
            async def get(self, *a, **k):
                raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _ConnectClient([]))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(_download_import_url("https://example.com/down", str(tmp_path / "dl.bin"), 1024 * 1024))
        assert ei.value.status_code == 502

    def test_redirect_loop_is_capped(self, monkeypatch, tmp_path):
        import asyncio
        from app.routes.upload import _download_import_url, IMPORT_MAX_REDIRECTS
        from fastapi import HTTPException
        fake = _FakeImportClient([
            (302, b"", {"location": f"https://example.com/{i}"}) for i in range(IMPORT_MAX_REDIRECTS + 2)
        ] + [(200, b"x", {})])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)

        with pytest.raises(HTTPException) as ei:
            asyncio.run(_download_import_url("https://example.com/loop", str(tmp_path / "dl.bin"), 1024 * 1024))
        assert ei.value.status_code == 400

    def test_rebinding_cannot_change_the_connected_ip(self, monkeypatch, tmp_path):
        """THE DNS-rebinding regression: a hostile resolver answering a public
        IP first then a private one (classic rebinding) can only influence the
        FIRST lookup. The hostname is resolved exactly once; the transport is
        built from that single validated answer, so the rebinding answer is
        never used."""
        import asyncio
        import socket
        import app.routes.upload as upload

        dns_calls = {"n": 0}

        def evil_dns(host, port, **kw):
            dns_calls["n"] += 1
            if dns_calls["n"] == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
            # Second lookup would rebind to a private address — must never happen.
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 0))]
        monkeypatch.setattr(socket, "getaddrinfo", evil_dns)

        captured = {}

        class _FakePinnedTransport:
            def __init__(self, pinned_ip, **kw):
                captured["ip"] = pinned_ip
        monkeypatch.setattr(upload, "_PinnedHTTPTransport", _FakePinnedTransport)

        fake = _FakeImportClient([(200, b"abc", {"content-type": "video/mp4"})])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)

        ct, total = asyncio.run(upload._download_import_url(
            "https://example.com/f.mp4", str(tmp_path / "dl.bin"), 1024 * 1024
        ))
        assert total == 3
        assert dns_calls["n"] == 1  # resolved exactly once — the rebind answer was never used
        assert captured["ip"] == "93.184.216.34"  # the FIRST (public) answer is what gets connected to

    def test_overall_wallclock_timeout_enforced(self, monkeypatch, tmp_path):
        """Per-request timeouts don't bound a slow trickle — the whole download
        is capped by asyncio.timeout (here shrunk to 50ms to test fast)."""
        import asyncio
        import app.routes.upload as upload
        from app.routes.upload import _download_import_url
        from fastapi import HTTPException

        monkeypatch.setattr(upload, "IMPORT_MAX_TOTAL_SECONDS", 0.2)

        class _SlowClient(_FakeImportClient):
            async def get(self, *a, **k):
                await asyncio.sleep(5)
                return _FakeImportResponse(200, b"x", {})

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _SlowClient([]))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(_download_import_url(
                "https://example.com/slow", str(tmp_path / "dl.bin"), 1024 * 1024
            ))
        assert ei.value.status_code == 504


class TestImportRoute:
    """POST /upload/import e2e through the shared pipeline."""

    URL = "/upload/import"
    MP4_BYTES = b"\x00\x00\x00\x20ftypmp42" + b"x" * 2048

    def _mock_pipeline(self, mock_conn, dedup_url=None):
        """The _store_and_finalize fetchrow sequence for a no-key import:
        user, usage, (dedup hit | None), insert."""
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},  # user
            None,  # usage record
            ({"minio_url": dedup_url} if dedup_url else None),  # content-dedup
            {"id": UUID("22222222-2222-4222-8222-222222222222")},  # INSERT RETURNING id
        ]

    def test_import_success_with_url_derived_filename(self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch):
        self._mock_pipeline(mock_conn)
        _patch_import_client(monkeypatch, [
            (200, self.MP4_BYTES, {"content-type": "video/mp4"}),
        ])

        resp = client.post(self.URL, json={"url": "https://example.com/videos/s01e01.mp4"}, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        # file_id is a freshly generated UUID (the insert mock's RETURNING value
        # is ignored — the record is keyed by the generated id).
        assert UUID(data["file_id"])
        assert data["filename"] == "s01e01.mp4"
        assert "magnet_link" in data
        assert "embed_url" in data
        # The imported bytes went through the SAME storage path as uploads.
        assert mock_s3_client.create_multipart_upload.called
        assert mock_s3_client.complete_multipart_upload.called

    def test_import_explicit_filename_and_idempotency_key(self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch):
        # With an idempotency key: completed-by-key check, user, usage, dedup, insert.
        mock_conn.fetchrow.side_effect = [
            None,  # idempotency dedup — nothing stored for this key yet
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,  # usage
            None,  # content-dedup
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]
        _patch_import_client(monkeypatch, [
            (200, self.MP4_BYTES, {"content-type": "video/mp4"}),
        ])

        resp = client.post(
            self.URL,
            json={"url": "https://example.com/clip.mp4", "filename": "my show.webm", "idempotency_key": "import-key-1"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["filename"] == "my_show.webm"  # sanitized
        # The INSERT carried the idempotency key + content hash.
        insert_args = mock_conn.fetchrow.call_args_list[-1].args
        assert insert_args[-2] == "import-key-1"
        assert len(insert_args[-1]) == 64

    def test_import_dedup_reuses_existing_copy(self, client, auth_headers, mock_conn, mock_s3_client, monkeypatch):
        """Identical imported bytes store ONCE: the fresh object is deleted and
        the row points at the existing copy — dedup crosses upload paths."""
        existing_url = "https://storage.example/bucket/other-user/other-file/original.mp4"
        self._mock_pipeline(mock_conn, dedup_url=existing_url)
        _patch_import_client(monkeypatch, [
            (200, self.MP4_BYTES, {"content-type": "video/mp4"}),
        ])

        resp = client.post(self.URL, json={"url": "https://example.com/again.mp4"}, headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["minio_url"] == existing_url
        delete_calls = [c.kwargs["Key"] for c in mock_s3_client.delete_object.call_args_list]
        assert len(delete_calls) == 1
        assert delete_calls[0].endswith("/again.mp4")

    def test_import_blocks_private_url(self, client, auth_headers):
        resp = client.post(
            self.URL,
            json={"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "private/internal" in resp.json()["detail"]

    def test_import_redirect_to_private_blocked(self, client, auth_headers, monkeypatch):
        fake = _patch_import_client(monkeypatch, [
            (302, b"", {"location": "http://10.0.0.5/internal"}),
            (200, b"x", {}),
        ])
        resp = client.post(self.URL, json={"url": "https://example.com/r"}, headers=auth_headers)
        assert resp.status_code == 400
        assert len(fake.calls) == 1  # the private hop was never requested

    def test_import_empty_file_rejected(self, client, auth_headers, monkeypatch):
        _patch_import_client(monkeypatch, [(200, b"", {"content-type": "video/mp4"})])
        resp = client.post(self.URL, json={"url": "https://example.com/empty.mp4"}, headers=auth_headers)
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"]

    def test_import_unsupported_type_rejected(self, client, auth_headers, monkeypatch):
        _patch_import_client(monkeypatch, [(200, b"data", {"content-type": "application/octet-stream"})])
        resp = client.post(self.URL, json={"url": "https://example.com/data.bin"}, headers=auth_headers)
        assert resp.status_code == 400
        assert "supported file type" in resp.json()["detail"]

    def test_import_oversized_rejected(self, client, auth_headers, monkeypatch, mock_conn):
        import app.routes.upload as upload
        monkeypatch.setattr(upload.settings, "max_upload_size_mb", 0.001)  # ~1 KB
        _patch_import_client(monkeypatch, [(200, b"z" * 2048, {"content-type": "video/mp4"})])
        resp = client.post(self.URL, json={"url": "https://example.com/big.mp4"}, headers=auth_headers)
        assert resp.status_code == 413

    def test_import_timeout_returns_504(self, client, auth_headers, monkeypatch):
        class _TimeoutClient(_FakeImportClient):
            async def get(self, *a, **k):
                raise httpx.TimeoutException("slow")
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _TimeoutClient([]))
        resp = client.post(self.URL, json={"url": "https://example.com/slow.mp4"}, headers=auth_headers)
        assert resp.status_code == 504

    def test_import_requires_auth(self, client):
        resp = client.post(self.URL, json={"url": "https://example.com/x.mp4"})
        assert resp.status_code == 401

    def test_import_malformed_url_rejected(self, client, auth_headers):
        """Non-http(s) or garbage URLs get a clear 400 before any fetch — they
        must NOT fall through to the misleading 'private/internal' message."""
        for bad in ("not a url", "ftp://example.com/x.mp4", "file:///etc/passwd", "   "):
            resp = client.post(self.URL, json={"url": bad}, headers=auth_headers)
            assert resp.status_code == 400, bad
            assert "http(s)" in resp.json()["detail"], bad

    def test_import_magic_byte_rejection(self, client, auth_headers, monkeypatch):
        """Imported bytes get the SAME magic-byte check as a direct upload — a
        file whose content doesn't match its extension is rejected."""
        import app.routes.upload as upload
        monkeypatch.setattr(upload, "validate_file_magic", lambda fs, fn: (False, "Content does not match .mp4"))
        _patch_import_client(monkeypatch, [
            (200, b"\x00\x00\x00\x20ftypmp42" + b"x" * 64, {"content-type": "video/mp4"}),
        ])
        resp = client.post(self.URL, json={"url": "https://example.com/evil.mp4"}, headers=auth_headers)
        assert resp.status_code == 400
        assert "rejected" in resp.json()["detail"]

    def test_import_zip_bomb_rejected(self, client, auth_headers, monkeypatch):
        """Compression-ratio (zip bomb) check applies to imported files too."""
        import app.routes.upload as upload
        monkeypatch.setattr(upload, "estimate_compression_ratio", lambda fs: 1000)
        _patch_import_client(monkeypatch, [(200, self.MP4_BYTES, {"content-type": "video/mp4"})])
        resp = client.post(self.URL, json={"url": "https://example.com/bomb.mp4"}, headers=auth_headers)
        assert resp.status_code == 400
        assert "compression ratio" in resp.json()["detail"]

    def test_import_rate_limit_peek_blocks_before_download(self, client, auth_headers, mock_redis, monkeypatch):
        """When the per-IP upload budget is already exhausted, the import is
        rejected BEFORE the server-side download starts (no wasted fetch)."""
        mock_redis.get.return_value = "10"  # budget exhausted

        def _explode(*a, **k):
            raise AssertionError("download must not start when rate-limited")
        monkeypatch.setattr(httpx, "AsyncClient", _explode)

        resp = client.post(self.URL, json={"url": "https://example.com/x.mp4"}, headers=auth_headers)
        assert resp.status_code == 429

    def test_import_exceeds_usage_limit(self, client, auth_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            {"total_gb_used": 100.0},  # 100GB cap already hit
        ]
        _patch_import_client(monkeypatch, [(200, b"x" * 1024 * 1024, {"content-type": "video/mp4"})])
        resp = client.post(self.URL, json={"url": "https://example.com/full.mp4"}, headers=auth_headers)
        assert resp.status_code == 402
        assert "Usage limit exceeded" in resp.json()["detail"]
