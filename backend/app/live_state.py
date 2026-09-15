"""Ephemeral live state — Redis-first with TTLs, in-memory fallback.

SHORT-LIVED live state (presence, availability, heartbeats, channel presence,
latest segment, monitor locks) intentionally does NOT live in Postgres: it
changes every second, must self-expire, and survives nothing important. Redis
keys all carry TTLs so a crashed viewer or a dead MediaMTX can never leave
stale peers behind (the "dead peers linger forever" failure mode).

Redis is an OPTIONAL dependency in this codebase (fail-open get_redis()
pattern); every function here therefore also maintains a bounded in-memory
mirror so a Redis outage degrades to single-process operation instead of
breaking live playback. Key shape (all with TTLs):

    live:v1:chan:{slug}:meta            hash  latest_seq/last_seg_ts/bitrate/...
    live:v1:chan:{slug}:seg:{seq}       hash  ts/size (segment registry entry)
    live:v1:chan:{slug}:avail:{seq}     set   peer_ids that possess the segment
    live:v1:chan:{slug}:peer:{peer}     hash  peer health/counters (TTL = peer_timeout)
    live:v1:chan:{slug}:peers           set   active peer ids (index for expiry)
    live:v1:chan:{slug}:viewers         set   active viewer session ids
    live:v1:chan:{slug}:counters        hash  bytes_p2p/bytes_origin/... (rolling)
    live:v1:lock:monitor                string  monitor single-flight lock
"""
import asyncio
import json
import logging
import time
from typing import Any

from .database import get_redis

logger = logging.getLogger(__name__)

KV = "live:v1"

# Bounds for the in-memory fallback so an attacker-controlled slug can't grow
# this process's memory without bound when Redis is down (same philosophy as
# security.enforce_max_size). Per-channel maps; stale channels are pruned when
# their meta entry expires.
_MEMORY_MAX_CHANNELS = 200
_MEMORY_MAX_SEGMENTS_PER_CHANNEL = 600

# Last sequence number observed per channel in the fallback mirror. Kept
# outside _mem so expiry paths can't lose it (drives live-edge recovery).
_mem_last_seq: dict[str, int] = {}

# Monitor bookkeeping (module-level so tests can inspect without a running loop)
_monitor_task: asyncio.Task | None = None
_monitor_last_ok: float = 0.0
_monitor_last_error: str | None = None


def _mem() -> dict:
    """The in-memory fallback mirror: {slug: {kind: {key: (value, expires_at)}}}."""
    global _mem_store
    try:
        return _mem_store
    except NameError:
        _mem_store = {}
        return _mem_store


def _prune_mem() -> None:
    store = _mem()
    now = time.time()
    for slug in list(store.keys()):
        ch = store[slug]
        live_any = False
        for kind in list(ch.keys()):
            entries = ch[kind]
            for k in list(entries.keys()):
                exp = entries[k][1] if isinstance(entries[k], tuple) else 0
                if exp and exp < now:
                    del entries[k]
                else:
                    live_any = True
            if not entries:
                del ch[kind]
        if not live_any and slug not in _mem_last_seq:
            del store[slug]
    while len(store) > _MEMORY_MAX_CHANNELS:
        oldest = next(iter(store))
        del store[oldest]


def _mem_set(slug: str, kind: str, key: str, value: Any, ttl: float) -> None:
    store = _mem()
    ch = store.setdefault(slug, {})
    ch.setdefault(kind, {})[key] = (value, time.time() + ttl)
    _prune_mem()


def _mem_get(slug: str, kind: str, key: str) -> Any | None:
    store = _mem()
    entry = store.get(slug, {}).get(kind, {}).get(key)
    if not entry:
        return None
    value, exp = entry
    if exp < time.time():
        store.get(slug, {}).get(kind, {}).pop(key, None)
        return None
    return value


def _mem_del(slug: str, kind: str, key: str) -> None:
    store = _mem()
    store.get(slug, {}).get(kind, {}).pop(key, None)


def _mem_members(slug: str, kind: str) -> list[str]:
    """All live keys of a kind for a channel (set/dict members)."""
    store = _mem()
    entries = store.get(slug, {}).get(kind, {})
    now = time.time()
    out = []
    for k, entry in list(entries.items()):
        exp = entry[1] if isinstance(entry, tuple) else 0
        if exp and exp < now:
            del entries[k]
        else:
            out.append(k)
    return out


# ── Redis failure isolation ──────────────────────────────────────────────────

# Redis return types the callers understand. Anything else (a mock leak, an
# unexpected client wrapper) is treated as "no data" so the caller falls back
# to the in-memory mirror instead of iterating garbage.
_REDIS_OK_TYPES = (str, bytes, int, float, bool, list, set, dict, tuple, type(None))


def _normalize_redis_result(result):
    """Coerce a Redis lambda's result into a caller-safe value or None.

    - bare coroutines (mispatched mocks) are awaited;
    - non-Redis-shaped objects (MagicMock leaking through a partial patch)
      become None — the caller then uses the in-memory fallback."""
    if asyncio.iscoroutine(result):
        try:
            result = asyncio.get_event_loop().run_until_complete(result)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(result, _REDIS_OK_TYPES):
        return result
    return None


async def _redis_call(fn, *args, **kwargs):
    """Run a Redis lambda; swallow failures (return None) — live state is
    best-effort and the in-memory mirror covers outages."""
    try:
        redis = await get_redis()
        if redis is None:
            return None
        result = await fn(redis)
        if asyncio.iscoroutine(result):
            # A missed patch (or a misconfigured mock) can hand us a bare
            # coroutine — await it so the caller never receives one.
            result = await result
        if isinstance(result, _REDIS_OK_TYPES):
            return result
        return None
    except Exception as e:  # noqa: BLE001 — degrade, never crash a viewer request
        logger.debug("live_state redis error: %s", e)
        return None


# ── Channel meta (latest segment, bitrate, uptime, status hints) ─────────────

async def set_channel_meta(slug: str, **fields: Any) -> None:
    """Merge fields into the channel's ephemeral meta (TTL refresh)."""
    ttl = 60
    current = await _redis_call(
        lambda r: r.hgetall(f"{KV}:chan:{slug}:meta")
    ) or {}
    current.update({k: str(v) for k, v in fields.items()})
    async def _w(r):
        pipe = r.pipeline()
        pipe.delete(f"{KV}:chan:{slug}:meta")
        if current:
            pipe.hset(f"{KV}:chan:{slug}:meta", mapping=current)
            pipe.expire(f"{KV}:chan:{slug}:meta", ttl)
        await pipe.execute()
    await _redis_call(_w)
    mem_meta = _mem_get(slug, "meta", "data") or {}
    mem_meta.update(fields)
    _mem_set(slug, "meta", "data", mem_meta, ttl)


async def get_channel_meta(slug: str) -> dict:
    raw = await _redis_call(lambda r: r.hgetall(f"{KV}:chan:{slug}:meta"))
    if raw is None:
        raw = _mem_get(slug, "meta", "data") or {}
    # asyncpg-style str values throughout
    return {k: v for k, v in (raw or {}).items()}


async def clear_channel(slug: str) -> None:
    """Drop a channel's whole ephemeral state (stream end / channel delete)."""
    await _redis_call(lambda r: _delete_channel_keys(r, slug))
    store = _mem()
    store.pop(slug, None)
    _mem_last_seq.pop(slug, None)


def _delete_channel_keys(redis, slug: str):
    """Delete all keys for a channel via SCAN (works on cluster-safe patterns)."""
    async def _run():
        pattern = f"{KV}:chan:{slug}:*"
        async for key in redis.scan_iter(match=pattern, count=100):
            await redis.delete(key)
    return _run()


# ── Segment registry (live window) ───────────────────────────────────────────

async def register_segment(slug: str, seq: int, duration: float = 0.0, size: int = 0) -> None:
    """Record a segment as present at origin. The entry self-expires after the
    live retention window — stale segments can never be served as live."""
    from .live_settings import get_live_settings

    s = get_live_settings()
    retention = s.segment_retention
    key = f"{KV}:chan:{slug}:seg:{seq}"
    payload = json.dumps({"ts": time.time(), "duration": duration, "size": size})
    await _redis_call(lambda r: r.setex(key, retention, payload))

    # Memory mirror with the bounded window.
    segments = _mem_members(slug, "segs")
    if len(segments) >= _MEMORY_MAX_SEGMENTS_PER_CHANNEL:
        seqs = sorted(int(k.split(":")[-1]) for k in segments)
        for old in seqs[: len(seqs) - _MEMORY_MAX_SEGMENTS_PER_CHANNEL + 1]:
            _mem_del(slug, "segs", str(old))
            # Mirror availability cleanup for dropped segments
            _mem_del(slug, "avail", str(old))
    _mem_set(slug, "segs", str(seq), payload, retention)
    if seq > _mem_last_seq.get(slug, -1):
        _mem_last_seq[slug] = seq
    await set_channel_meta(slug, latest_seq=str(seq), last_seg_ts=str(time.time()))


async def segment_exists(slug: str, seq: int) -> bool:
    val = await _redis_call(lambda r: r.get(f"{KV}:chan:{slug}:seg:{seq}"))
    if val is None:
        val = _mem_get(slug, "segs", str(seq))
    return val is not None


async def latest_segment(slug: str) -> int | None:
    """Newest registered segment sequence (None when the channel has none)."""
    meta = await get_channel_meta(slug)
    try:
        return int(meta.get("latest_seq", -1)) if meta.get("latest_seq") is not None else None
    except (TypeError, ValueError):
        return _mem_last_seq.get(slug)


async def prune_expired_segments(slug: str) -> int:
    """Expire segment registry + availability entries that left the live
    window. Returns the number pruned (monitor calls this per channel)."""
    from .live_settings import get_live_settings

    s = get_live_settings()
    cutoff = time.time() - s.segment_retention
    pruned = 0
    for kind in ("segs", "avail"):
        # Scan the raw store (NOT _mem_members, which self-expires silently —
        # the prune must COUNT what it reclaims so the monitor can log it).
        entries = _mem().get(slug, {}).get(kind, {})
        for key in list(entries.keys()):
            raw = entries.get(key)
            value = raw[0] if isinstance(raw, tuple) else raw
            exp = raw[1] if isinstance(raw, tuple) else 0
            stale = bool(exp and exp < time.time())
            ts = None
            if isinstance(value, str):
                try:
                    ts = json.loads(value).get("ts")
                except (ValueError, TypeError):
                    ts = None
            if stale or ts is None or float(ts) < cutoff:
                del entries[key]
                pruned += 1
    return pruned


# ── Peer availability + health ───────────────────────────────────────────────

async def peer_report(slug: str, peer_id: str, session_id: str, haves: list[int],
                      counters: dict | None = None) -> None:
    """Record a peer's availability report + heartbeat in one shot."""
    from .live_settings import get_live_settings

    s = get_live_settings()
    ttl = s.peer_timeout
    peer_key = f"{KV}:chan:{slug}:peer:{peer_id}"
    counters = counters or {}

    async def _w(r):
        pipe = r.pipeline()
        pipe.hset(peer_key, mapping={
            "session_id": session_id,
            "last_seen": str(time.time()),
            **{k: str(v) for k, v in counters.items()},
        })
        pipe.expire(peer_key, ttl)
        pipe.sadd(f"{KV}:chan:{slug}:peers", peer_id)
        pipe.expire(f"{KV}:chan:{slug}:peers", ttl)
        pipe.sadd(f"{KV}:chan:{slug}:viewers", session_id)
        pipe.expire(f"{KV}:chan:{slug}:viewers", ttl)
        for seq in haves:
            pipe.sadd(f"{KV}:chan:{slug}:avail:{seq}", peer_id)
            pipe.expire(f"{KV}:chan:{slug}:avail:{seq}", s.segment_retention)
        await pipe.execute()

    wrote = await _redis_call(_w)
    # Memory mirror (always — fallback + read-your-writes in tests)
    _mem_set(slug, "peer", peer_id, {"session_id": session_id, "last_seen": time.time(), **counters}, ttl)
    if session_id:
        _mem_set(slug, "viewers", session_id, 1, ttl)
    _mem_set(slug, "peerset", peer_id, 1, ttl)
    for seq in haves or []:
        entry = _mem_get(slug, "avail", str(seq)) or set()
        entry.add(peer_id)
        _mem_set(slug, "avail", str(seq), entry, s.segment_retention)
    if wrote is None and not haves:
        return


async def mark_peer_dead(slug: str, peer_id: str) -> None:
    """Remove a peer from all availability sets (transfer failure hard-fail)."""
    await _redis_call(lambda r: _remove_peer_keys(r, slug, peer_id))
    _mem_del(slug, "peer", peer_id)
    _mem_del(slug, "peerset", peer_id)
    for key in _mem_members(slug, "avail"):
        entry = _mem_get(slug, "avail", key)
        if isinstance(entry, set) and peer_id in entry:
            entry.discard(peer_id)
            _mem_set(slug, "avail", key, entry, 60)


def _remove_peer_keys(redis, slug: str, peer_id: str):
    async def _run():
        peer_key = f"{KV}:chan:{slug}:peer:{peer_id}"
        await redis.delete(peer_key)
        await redis.srem(f"{KV}:chan:{slug}:peers", peer_id)
        # Strip from every availability set in the live window
        pattern = f"{KV}:chan:{slug}:avail:*"
        async for key in redis.scan_iter(match=pattern, count=100):
            await redis.srem(key, peer_id)
    return _run()


async def active_peers(slug: str) -> list[str]:
    ids = await _redis_call(lambda r: r.smembers(f"{KV}:chan:{slug}:peers"))
    if ids is None:
        return list(_mem_members(slug, "peerset"))
    # Drop peers whose individual TTL expired even if the index lingers
    out = []
    for pid in ids:
        if await _redis_call(lambda r, p=pid: r.exists(f"{KV}:chan:{slug}:peer:{p}")):
            out.append(pid)
    return out


async def active_viewers(slug: str) -> int:
    n = await _redis_call(lambda r: r.scard(f"{KV}:chan:{slug}:viewers"))
    if n is None:
        n = len(_mem_members(slug, "viewers"))
    return int(n or 0)


async def peers_with_segment(slug: str, seq: int) -> list[str]:
    ids = await _redis_call(lambda r: r.smembers(f"{KV}:chan:{slug}:avail:{seq}"))
    if ids is None:
        entry = _mem_get(slug, "avail", str(seq))
        return list(entry) if isinstance(entry, set) else []
    out = []
    for pid in ids:
        if await _redis_call(lambda r, p=pid: r.exists(f"{KV}:chan:{slug}:peer:{p}")):
            out.append(pid)
    return out


async def peer_health(slug: str, peer_id: str) -> dict | None:
    raw = await _redis_call(lambda r: r.hgetall(f"{KV}:chan:{slug}:peer:{peer_id}"))
    if raw is None:
        raw = _mem_get(slug, "peer", peer_id)
    return dict(raw or {})


async def peer_scores(slug: str, seq: int) -> list[tuple[str, float]]:
    """Rank peers that have a segment: healthy first, then by upload volume
    (a peer serving bytes is a proven-good peer). Never raises."""
    peers = await peers_with_segment(slug, seq)
    scored: list[tuple[str, float]] = []
    now = time.time()
    for pid in peers:
        h = await peer_health(slug, pid) or {}
        try:
            last_seen = float(h.get("last_seen", 0))
        except (TypeError, ValueError):
            last_seen = 0
        age = max(0.0, now - last_seen)
        if age > 30:
            continue  # stale → not selectable
        try:
            served = float(h.get("segments_served", 0) or 0)
        except (TypeError, ValueError):
            served = 0.0
        scored.append((pid, served - age))
    scored.sort(key=lambda t: -t[1])
    return scored


# ── Rolling counters (dashboard + analytics rollup) ──────────────────────────

async def bump_counters(slug: str, **deltas: int) -> None:
    """Increment rolling per-channel counters (bytes, segments, errors...)."""
    key = f"{KV}:chan:{slug}:counters"

    async def _w(r):
        pipe = r.pipeline()
        for k, v in deltas.items():
            if v:
                pipe.hincrby(key, k, int(v))
        pipe.expire(key, 600)
        await pipe.execute()

    await _redis_call(_w)
    mem = _mem_get(slug, "counters", "data") or {}
    for k, v in deltas.items():
        mem[k] = int(mem.get(k, 0)) + int(v or 0)
    _mem_set(slug, "counters", "data", mem, 600)


async def read_counters(slug: str) -> dict:
    raw = await _redis_call(lambda r: r.hgetall(f"{KV}:chan:{slug}:counters"))
    if raw is None:
        raw = _mem_get(slug, "counters", "data") or {}
    out = {}
    for k, v in (raw or {}).items():
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            out[k] = 0
    return out


async def reset_counters(slug: str) -> dict:
    """Read-and-clear the rolling counters (monitor rollup into Postgres)."""
    out = await read_counters(slug)
    await _redis_call(lambda r: r.delete(f"{KV}:chan:{slug}:counters"))
    _mem_del(slug, "counters", "data")
    return out


# ── Monitor (status transitions, expiry, analytics rollup) ───────────────────

async def _monitor_tick() -> None:
    """One monitor pass. All DB work lives in routes/live (import here to keep
    this module DB-free); failures per channel are isolated."""
    from .routes.live import monitor_channel_status, rollup_channel_analytics
    from .live_settings import get_live_settings

    interval = get_live_settings().monitor_interval
    slugs = set(_mem_last_seq.keys())
    # Discover Redis-side channels too (registered segments imply activity)
    redis = await get_redis()
    if redis is not None:
        try:
            async for key in redis.scan_iter(match=f"{KV}:chan:*:meta", count=100):
                parts = key.split(":")
                if len(parts) >= 4:
                    slugs.add(parts[3])
        except Exception:
            pass
    for slug in sorted(slugs):
        try:
            await monitor_channel_status(slug)
            await rollup_channel_analytics(slug)
        except Exception as e:  # noqa: BLE001 — per-channel isolation
            logger.warning("live monitor: channel %s failed: %s", slug, e)
    # Opportunistic per-channel segment expiry
    for slug in sorted(slugs):
        try:
            await prune_expired_segments(slug)
        except Exception:
            pass
    del interval


def get_live_monitor_health() -> dict:
    from .live_settings import get_live_settings

    s = get_live_settings()
    return {
        "monitor_running": _monitor_task is not None and not _monitor_task.done(),
        "mediamtx_configured": bool(s.mediamtx_api_url),
        "mediamtx_reachable": (time.time() - _monitor_last_ok) < 30 if _monitor_last_ok else False,
        "active_channels": len(_mem_last_seq),
        "last_error": _monitor_last_error,
    }


def start_live_monitor() -> None:
    """Start the background monitor exactly once per process (idempotent).

    Never raises: the monitor is telemetry/lifecycle sugar — live status also
    updates on-demand (auth hook, availability reports) so a dead monitor
    degrades freshness but not correctness.
    """
    global _monitor_task, _monitor_started
    try:
        if _monitor_task is not None and not _monitor_task.done():
            return
        _monitor_task = asyncio.get_event_loop().create_task(_monitor_loop())
        _monitor_started = True
    except Exception as e:  # noqa: BLE001
        logger.warning("live monitor failed to start: %s", e)


async def _monitor_loop() -> None:
    global _monitor_last_ok, _monitor_last_error
    from .live_settings import get_live_settings

    interval = max(2, get_live_settings().monitor_interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await _monitor_tick()
            _monitor_last_ok = time.time()
            _monitor_last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the monitor must never die
            _monitor_last_error = str(e)[:200]
            logger.warning("live monitor tick error: %s", e)
