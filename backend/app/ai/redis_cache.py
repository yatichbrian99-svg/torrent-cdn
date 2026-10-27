"""Redis-backed replay cache and attestation nonce store.

Both classes implement the same protocols as the in-process doubles
(``MemoryReplayCache``, ``NonceStore``) so the coordinator can swap the
backing store without changing call sites — it only chooses the Redis version
when a Redis client is available and falls back to the in-process double when
it is not.

Assumptions recorded in LIMITATIONS.md:
* A multi-instance deployment must use these Redis-backed versions or replay /
  nonce protection becomes process-local.
* Keys are namespaced under ``tcdn-ai:`` to avoid colliding with any other
  Redis usage in the same Redis instance.
* Both are fail-closed on write errors only when the operation is security-
  critical (nonce consume, replay claim); a Redis outage on issuance degrades
  to the in-process fallback so a transient Redis blip cannot block placement
  or attestation issuance.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from . import clock
from .errors import AttestationError
from .nonce_store import NonceStore

logger = logging.getLogger(__name__)

REDIS_PREFIX = "tcdn-ai:"

#: Atomic fetch-and-delete for servers/clients without ``GETDEL`` (Redis < 6.2).
#: ``consume`` used to issue GET and then DEL, which is a race: two concurrent
#: consumers both read the value and both succeed. Measured against a real Redis
#: with two barrier-released consumers: 122/300 trials at no gap, 299/300 with
#: the window widened to 5 ms — the window is the two round trips, so an
#: attacker only has to race the verifier, not guess a secret. GETDEL is one
#: round trip and one server-side operation, so the window does not exist.
_GETDEL_LUA = """
local value = redis.call('GET', KEYS[1])
if value then
  redis.call('DEL', KEYS[1])
end
return value
""".strip()

#: Per-operation failure counters. With Redis configured these code paths ARE
#: the security control (single-use tickets/nonces, shared caps), so a failure
#: is an incident a deployment must be able to alert on, not a debug detail.
#: Export this from wherever the deployment scrapes metrics, e.g.
#: ``for op, n in redis_failure_counts().items(): gauge("tcdn_ai_redis_failures",
#: n, operation=op)``.
_FAILURE_COUNTS: Counter[str] = Counter()

#: Process-wide alert seam. The coordinator registers a hook that writes an
#: ``ai.redis_unavailable`` audit row; a deployment can register its own
#: (Sentry, PagerDuty, a Prometheus counter) instead of or as well.
_failure_hook: Callable[[dict[str, Any]], None] | None = None


def redis_failure_counts() -> dict[str, int]:
    """Snapshot of the per-operation failure counters (metrics/alerting)."""
    return dict(_FAILURE_COUNTS)


def set_redis_failure_hook(hook: Callable[[dict[str, Any]], None] | None) -> None:
    """Register (or clear) the process-wide failure hook."""
    global _failure_hook
    _failure_hook = hook


def reset_redis_failure_state() -> None:
    """Clear the counters and the hook. Tests only."""
    _FAILURE_COUNTS.clear()
    set_redis_failure_hook(None)


def _key_fingerprint(key: str) -> str:
    """A stable, non-reversible label for a key, so logs carry no key material."""
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:12]


def note_redis_failure(
    operation: str,
    key: str,
    error: BaseException | None,
    hook: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Record one Redis failure: ERROR log + counter + alert hook.

    Never raises: the caller is already on a fail-closed path and must not lose
    its own error to a broken alert sink. No secret or key material is logged —
    only a fingerprint of the key and the exception class name.
    """
    fingerprint = _key_fingerprint(key)
    error_name = type(error).__name__ if error is not None else "unavailable"
    logger.error(
        "tcdn-ai redis %s failed (key_fingerprint=%s, error=%s) — failing closed",
        operation,
        fingerprint,
        error_name,
    )
    _FAILURE_COUNTS[operation] += 1
    sink = hook or _failure_hook
    if sink is not None:
        try:
            sink(
                {
                    "operation": operation,
                    "key_fingerprint": fingerprint,
                    "error": error_name,
                }
            )
        except Exception:  # a broken alert hook must not mask the failure
            logger.exception("tcdn-ai redis failure hook raised")


#: Connection kwargs a blocking twin may inherit. The asyncio pool carries more
#: (single_connection_client, protocol, retry objects); passing those to the
#: blocking client is either invalid or changes semantics, so they are dropped.
_TWIN_KWARGS = (
    "host",
    "port",
    "db",
    "username",
    "password",
    "ssl",
    "ssl_cert_reqs",
    "ssl_ca_certs",
    "socket_timeout",
    "socket_connect_timeout",
    "client_name",
)


def _blocking_twin(client: Any) -> Any | None:
    """A blocking redis client equivalent to an ``redis.asyncio`` one.

    The sync bridge below runs each coroutine on a **fresh** event loop in a
    helper thread. An ``redis.asyncio`` client cannot be used that way: its
    connection pool is bound to the loop that opened the connections, so every
    command raises ``got Future attached to a different loop``. That exception
    was swallowed by the fail-closed branch, which meant that with Redis
    configured — the only configuration that gives cross-instance replay
    protection — *every* ticket claim and nonce consume failed, so placement
    refused every ticket as a replay.

    The fix keeps the synchronous call sites: for an asyncio client, derive a
    blocking client from the same connection pool settings and use it in the
    calling thread (no loop at all). Clients that are not ``redis.asyncio``
    (the in-process doubles used by tests) return None and keep the old path.

    Returns None when the settings cannot be derived, so the caller can fall
    back instead of pretending the claim succeeded.
    """
    kwargs = getattr(
        getattr(client, "connection_pool", None), "connection_kwargs", None
    )
    if not kwargs:
        return None
    try:
        import redis as blocking_redis
    except Exception:  # pragma: no cover - redis is a hard dependency of this module
        return None
    allowed = {key: value for key, value in kwargs.items() if key in _TWIN_KWARGS}
    if not allowed.get("host"):
        return None
    allowed.setdefault("decode_responses", False)
    try:
        allowed["db"] = int(allowed.get("db") or 0)
        return blocking_redis.Redis(**allowed)
    except Exception:  # pragma: no cover - malformed pool settings
        return None


def _run_sync(coro: Any) -> Any:
    """Drive a Redis coroutine to completion from synchronous code.

    The replay cache and nonce store are consumed by *synchronous* verification
    paths (``TicketService.verify``, ``AttestationService.attest``), because the
    worker and the gateway need a verdict before they continue. Those call sites
    may or may not already be inside an event loop, so this helper handles both:

    * no running loop → ``asyncio.run``;
    * running loop → run the coroutine on a private loop in a helper thread.

    Blocking is deliberate: a replay claim that is not awaited is not a replay
    claim, so the check has to finish before the caller continues. Previously
    these methods were ``async`` but called without ``await``, which returned a
    truthy coroutine object — so ticket replay protection and single-use
    attestation nonces were silently disabled whenever Redis was configured.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    outcome: dict[str, Any] = {}

    def _runner() -> None:
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # propagated to the caller below
            outcome["error"] = exc

    thread = threading.Thread(target=_runner, name="redis-sync-bridge", daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class RedisReplayCache:  # pragma: no cover - exercised through coordinator break-tests
    """Redis-backed single-use ticket replay cache.

    ``claim`` is atomic via SET NX with an expiry, so two coordinator instances
    cannot both accept the same ticket jti.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        key_prefix: str = "ticket",
        clock=clock.now_unix,
        on_failure: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._clock = clock
        self._twin: Any | None = None
        self._twin_checked = False
        #: ``claim``/``consume`` are synchronous, so concurrent threads can call
        #: them at once. Without this lock, a second thread can see
        #: ``_twin_checked`` set before ``_twin`` is assigned and fall through to
        #: the event-loop bridge — which fails closed, turning a race into
        #: "every ticket is a replay".
        self._twin_lock = threading.Lock()
        self._on_failure = on_failure

    def _key(self, key: str) -> str:
        return f"{REDIS_PREFIX}{self._key_prefix}:replay:{key}"

    def _blocking(self) -> Any | None:
        with self._twin_lock:
            if not self._twin_checked:
                self._twin = _blocking_twin(self._redis)
                self._twin_checked = True
            return self._twin

    def claim(self, ticket_jti: str, expires_at: int) -> bool:
        """Return True the first time ``ticket_jti`` is seen, False afterwards.

        The expiry is the same as the in-process version — the ticket's own TTL
        plus skew — so a replay window cannot outlive the ticket it protects.
        """
        twin = self._blocking()
        if twin is not None:
            key = self._key(ticket_jti)
            ttl = max(1, expires_at - int(self._clock()))
            try:
                return twin.set(key, "1", nx=True, ex=ttl) is not None
            except Exception as exc:  # Redis is optional by design
                # Fail closed AND signal it: with Redis configured, this branch
                # refusing every claim is indistinguishable from every ticket
                # being a replay, which is exactly the outage that used to be a
                # warning nobody saw.
                note_redis_failure("ticket_claim", ticket_jti, exc, self._on_failure)
                return False
        return _run_sync(self._claim(ticket_jti, expires_at))

    async def _claim(self, ticket_jti: str, expires_at: int) -> bool:
        key = self._key(ticket_jti)
        now = int(self._clock())
        ttl = max(1, expires_at - now)
        try:
            ok = await self._redis.set(key, "1", nx=True, ex=ttl)
        except Exception as exc:  # never crash the coordinator; fail closed
            note_redis_failure("ticket_claim", ticket_jti, exc, self._on_failure)
            return False
        return ok is not None


class RedisNonceStore(
    NonceStore
):  # pragma: no cover - exercised through coordinator break-tests
    """Redis-backed single-use attestation nonces with expiry.

    Mirrors the in-process ``NonceStore`` API but backs the state with Redis TTLs
    so expiry is enforced by Redis, not only by the coordinator's clock on the
    next read.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = 300,
        key_prefix: str = "nonce",
        clock=clock.now_unix,
        on_failure: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(ttl_seconds=ttl_seconds, clock=clock)
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._twin: Any | None = None
        self._twin_checked = False
        #: See ``RedisReplayCache._twin_lock``: the sync path is reachable from
        #: concurrent threads (the concurrency tests below drive it that way).
        self._twin_lock = threading.Lock()
        self._on_failure = on_failure

    def _key(self, nonce: str) -> str:
        return f"{REDIS_PREFIX}{self._key_prefix}:{nonce}"

    def _blocking(self) -> Any | None:
        with self._twin_lock:
            if not self._twin_checked:
                self._twin = _blocking_twin(self._redis)
                self._twin_checked = True
            return self._twin

    def issue(self, node_id: str) -> str:
        nonce = super().issue(node_id)
        twin = self._blocking()
        if twin is not None:
            expires_at = int(self._clock()) + self.ttl_seconds
            try:
                twin.set(
                    self._key(nonce), f"{node_id}:{expires_at}", ex=self.ttl_seconds
                )
            except Exception as exc:  # pragma: no cover - Redis outage path
                # Degraded, not refused: the nonce is still in this process's
                # store, so the same instance can spend it. Say so loudly.
                note_redis_failure("nonce_issue", nonce, exc, self._on_failure)
            return nonce
        _run_sync(self._publish(node_id, nonce))
        return nonce

    async def _publish(self, node_id: str, nonce: str) -> None:
        key = self._key(nonce)
        expires_at = int(self._clock()) + self.ttl_seconds
        value = f"{node_id}:{expires_at}"
        try:
            await self._redis.set(key, value, ex=self.ttl_seconds)
        except Exception as exc:
            note_redis_failure("nonce_issue", nonce, exc, self._on_failure)

    def consume(self, nonce: str, node_id: str) -> None:
        twin = self._blocking()
        if twin is not None:
            value = self._take_blocking(twin, nonce)
            self._accept(value, nonce, node_id)
            return
        _run_sync(self._consume(nonce, node_id))

    def _take_blocking(self, twin: Any, nonce: str) -> Any:
        """Atomically fetch AND delete the nonce, in one server-side operation.

        ``GETDEL`` exists in Redis 6.2+/redis-py 4.2+; the Lua script is the same
        operation for anything older. The previous GET-then-DELETE pair was two
        round trips, so two concurrent consumers both read the value and both
        were accepted: measured 122/300 trials against a real Redis (299/300 with
        a 5 ms window). See ``_GETDEL_LUA``. This is checklist 5.4's single-use
        claim, which the ticket cache already met via SET NX.
        """
        key = self._key(nonce)
        try:
            getdel = getattr(twin, "getdel", None)
            if getdel is not None:
                return getdel(key)
            return twin.eval(_GETDEL_LUA, 1, key)
        except Exception as exc:
            note_redis_failure("nonce_consume", nonce, exc, self._on_failure)
            raise AttestationError(
                "attestation nonce store is unavailable",
                code="nonce_store_unavailable",
            ) from exc

    async def _take_async(self, nonce: str) -> Any:
        key = self._key(nonce)
        try:
            getdel = getattr(self._redis, "getdel", None)
            if getdel is not None:
                return await getdel(key)
            return await self._redis.eval(_GETDEL_LUA, 1, key)
        except Exception as exc:
            note_redis_failure("nonce_consume", nonce, exc, self._on_failure)
            raise AttestationError(
                "attestation nonce store is unavailable",
                code="nonce_store_unavailable",
            ) from exc

    def _accept(self, value: Any, nonce: str, node_id: str) -> None:
        """Validate a nonce that has ALREADY been taken from Redis.

        Taking it first means every failure below has burned the nonce. That is
        the point: a concurrent pair cannot both see it —
        ``test_ai_redis.py::TestNonceConsumptionIsAtomic`` races eight consumers
        at one nonce and requires exactly one winner — and a wrong-node or
        expired presentation cannot leave it behind for a retry.
        """
        if value is None:
            raise AttestationError(
                "unknown or already-used attestation nonce", code="stale_nonce"
            )
        try:
            expected_node, expires_at = value.decode("utf-8").split(":", 1)
            expires_at = int(expires_at)
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            raise AttestationError(
                "malformed nonce record", code="stale_nonce"
            ) from exc
        if expected_node != node_id:
            raise AttestationError(
                "nonce was issued for a different node", code="nonce_node_mismatch"
            )
        if int(self._clock()) > expires_at:
            raise AttestationError("attestation nonce has expired", code="stale_nonce")
        # Redis is the authority. The inherited ``consume`` requires the nonce to
        # be in THIS process's dict, which is false when a second coordinator
        # instance issued the nonce — attestation then failed closed across
        # instances (checklist 5.4). Drop the local copy if this instance has one;
        # never require it.
        with self._lock:
            self._nonces.pop(nonce, None)

    async def _consume(self, nonce: str, node_id: str) -> None:
        value = await self._take_async(nonce)
        self._accept(value, nonce, node_id)


async def setifempty(
    redis_client: Any,
    key: str,
    value: str,
    *,
    ex: int | None = None,
) -> bool:
    """Set ``key`` to ``value`` only if it is currently absent.

    Returns ``True`` when the key was set (first caller wins), ``False`` when it
    already held a value. This is the primitive the email-verification auto-verify
    path uses so two concurrent registrations from the same inbox during a provider
    outage cannot both mark the account verified — only the first does.

    Redis is optional by design; when it is unavailable the function returns
    ``False`` and the caller falls back to the DB path, which is correct (the DB
    path is idempotent for that case).
    """
    redis_key = f"{REDIS_PREFIX}setifempty:{key}"
    try:
        ok = await redis_client.set(redis_key, value, nx=True, ex=ex)
        return ok is not None
    except Exception as exc:
        # Returning False is the documented fallback, but it must not be silent:
        # the caller cannot tell "someone else won the race" from "Redis is
        # gone", and both used to look identical in the logs (nothing).
        note_redis_failure("setifempty", key, exc)
        return False
