"""Connect-to-the-validated-IP HTTP transport — one implementation, two callers.

Resolving a hostname once, checking every answer, and then letting the HTTP
client resolve it again at connect time is the same as not checking at all:
a hostile or merely flapping resolver (DNS rebinding) can answer a public
address to the check and a private one to the socket. Both SSRF guards in this
app — the import-by-URL fetch (``routes/upload.py``) and the AI engine's origin
shard fetch (``ai/shards.py``) — therefore hand the validated address to this
transport, which dials that address and nothing else.

The request URL is never rewritten: httpcore still derives the ``Host`` header
and the TLS SNI/server-hostname from the original hostname, so certificate
validation is unchanged — only the TCP target is pinned. An explicit transport
also means environment proxies (``HTTP(S)_PROXY``) are INTENTIONALLY bypassed,
because a proxy resolves the hostname itself at connect time and would re-open
the rebinding window.

Version-sensitive by nature: ``httpcore``'s network-backend hook and httpx's
private ``create_ssl_context`` are pinned by the requirements lock
(httpcore 1.0.9 / httpx 0.28.1), so a bump must re-verify this module.
"""

from __future__ import annotations

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend
from httpx._config import create_ssl_context


class PinnedNetworkBackend(AnyIOBackend):
    """httpcore network backend that pins every TCP connect to a PRE-RESOLVED,
    SSRF-validated IP. The `host` argument (the untrusted hostname from the
    request URL) is deliberately ignored — it must never be resolved again at
    connect time. Everything else — timeouts, error mapping, socket options,
    unix sockets, sleep — is inherited from AnyIOBackend."""

    def __init__(self, pinned_ip: str) -> None:
        self._pinned_ip = pinned_ip

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        return await super().connect_tcp(
            self._pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


# httpx.Limits is a plain config object (no per-instance state), so one shared
# instance is safe across transports. Module-level instead of a call in the
# signature default.
_DEFAULT_HTTPX_LIMITS = httpx.Limits()


class PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx async transport whose connection pool connects ONLY to a pinned
    address (via :class:`PinnedNetworkBackend`). The request URL is never
    rewritten, so httpcore still derives the Host header and TLS
    SNI/server-hostname from the original hostname — certificate validation is
    unchanged, only the TCP target is pinned. httpx 0.28's AsyncHTTPTransport
    doesn't forward a network_backend kwarg, so the pool is rebuilt here (the
    rest of the transport — request mapping, timeouts, redirects — is
    untouched)."""

    def __init__(
        self,
        pinned_ip: str,
        verify: bool | str = True,
        trust_env: bool = False,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits = _DEFAULT_HTTPX_LIMITS,
        retries: int = 0,
    ) -> None:
        super().__init__(
            verify=verify,
            trust_env=trust_env,
            http1=http1,
            http2=http2,
            limits=limits,
            retries=retries,
        )
        # Swap in the pinned pool. The default-backend pool built by
        # super().__init__ is entered but never used (no connections were made)
        # and is garbage-collected on reassignment — httpcore's async pool has
        # no sync __exit__, so it is simply dropped.
        ssl_context = create_ssl_context(verify=verify, trust_env=trust_env)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            retries=retries,
            network_backend=PinnedNetworkBackend(pinned_ip),
        )
        # The pool is entered via the transport's __aenter__ (httpcore's async
        # pool only implements __aenter__/__aexit__, not the sync protocol).
