import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import get_current_user_id
from ..config import get_settings
from ..database import get_db_pool, get_redis
from ..models import AnalyticsEventCreate
from ..security import enforce_max_size, get_client_ip

logger = logging.getLogger(__name__)

VALID_EVENTS = {"peer_joined", "peer_left", "chunk_downloaded"}

# A viewer can never download more bytes than the file contains, so a per-event
# byte counter beyond a small multiple of the FILE's size is analytics
# tampering. The pydantic model's absolute 2GB cap is the outer bound; this
# RELATIVE bound is what actually stops a public unauthenticated poster from
# inflating a file's stats with near-2GB events at 600/min/IP. The floor keeps
# tiny files (small test clips, short audios) from ever tripping a legit
# bursty client that reports a whole file in one event.
ANALYTICS_EVENT_BYTES_MULTIPLIER = 10
ANALYTICS_EVENT_BYTES_FLOOR = 1024 * 1024  # 1 MiB

# ── Scale-out ingestion (200k concurrent viewers) ────────────────────────────
#
# The old path did a rate-limit check + a uploads SELECT + an INSERT per event
# on the request path. One viewer posts ~1 event/sec (client batching), so
# 200k concurrent viewers ≈ 200k SELECTs + 200k INSERTs per minute — the DB
# dies long before the bandwidth does. The hot path is now:
#
#   1. rate limit (Redis, unchanged)
#   2. per-file size cap from a Redis-cached copy of file_size_mb (no DB hit)
#   3. append the validated event to a Redis LIST (buffer) — O(1), no DB hit
#   4. a background task drains the buffer in batches (one INSERT per batch,
#      500 events) — ~1 DB write/sec instead of thousands
#
# Under saturation the buffer ALSO sheds load by SAMPLING chunk_downloaded
# events (peer_joined/peer_left are never dropped — views/active-peers must
# stay exact). A sampled-away event's byte counters are DROPPED, not merged —
# the aggregates are then a representative sample of delivered bytes, which is
# the honest cheap version of the truth (summing every byte while dropping
# events would UNDERCOUNT; this way the error is bounded and explainable).
#
# Redis-down degradation: the writer keeps the SAME batching (an in-memory
# deque with the same flush cadence and caps) so a Redis outage no longer
# puts an INSERT per event back on the request path — the failure mode flips
# from "DB falls over" to "last few seconds of events lost on process crash"
# (acceptable for analytics; it already loses tail events today).
ANALYTICS_BUFFER_KEY = "analytics:event_buffer"
ANALYTICS_BATCH_SIZE = 500           # events per INSERT
ANALYTICS_FLUSH_INTERVAL = 2.0       # seconds between drains
ANALYTICS_BUFFER_MAX = 200_000       # hard cap: shed (don't queue) beyond this
# Sampling kicks in when the buffer exceeds this many PENDING events and
# applies to chunk_downloaded only. peer_joined/peer_left always enqueue.
ANALYTICS_SAMPLE_THRESHOLD = 50_000
# chunk_downloaded events above the threshold are enqueued with probability
# 1/N — a 1-in-4 sample still bounds aggregate error at a few percent while
# cutting buffer growth 4x.
ANALYTICS_SAMPLE_N = 4
# In-memory fallback buffer (Redis down). Same flush loop drains both.
_memory_event_buffer: list[dict] = []
_buffer_task_started = False

settings = get_settings()
router = APIRouter(prefix="/analytics", tags=["analytics"])

# In-memory fallback for public analytics rate limiting (used when Redis is down)
_analytics_get_requests: dict[str, list[float]] = {}
_ANALYTICS_GET_WINDOW = 60
# Public/authed analytics GET budget per IP per minute. 120/min (~2/sec) is
# generous for the demo page's 5s stats polling (12/min) while still stopping
# scrapers from hammering the aggregate endpoint.
ANALYTICS_GET_LIMIT = 120
# In-memory fallback for the public POST /analytics/event limiter (used when
# Redis is down). This is a WRITE endpoint with no auth, so it MUST be rate
# limited even without Redis — otherwise an attacker can flood the analytics
# table with unbounded inserts while Redis is degraded. Bounded via pruning.
_analytics_event_requests: dict[str, list[float]] = {}
_ANALYTICS_EVENT_WINDOW = 60
# In-memory fallback for the per-IP per-file event limiter (Redis-down twin of
# the Redis key below). Without it the per-file 30/min cap silently vanishes
# during a Redis outage and a single IP can inflate a specific file's P2P
# offload stats at the IP-level budget (600/min) — exactly what the per-file
# cap exists to stop. Keyed "{ip}:{file_id}" so two files don't share budget.
_analytics_file_requests: dict[str, list[float]] = {}
_ANALYTICS_FILE_WINDOW = 60


def _prune_analytics_get_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _analytics_get_requests.items() if not v or now - v[-1] > _ANALYTICS_GET_WINDOW]
    for k in expired:
        del _analytics_get_requests[k]
    enforce_max_size(_analytics_get_requests)


async def _aggregate_file_analytics(conn, file_id: UUID) -> dict:
    """Aggregate delivery stats for one file into the shared analytics shape.

    Used by both the authed (ownership-scoped) and public endpoints so the two
    always return an identical payload.
    """
    result = await conn.fetchrow(
        """SELECT
               COUNT(*) FILTER (WHERE event_type = 'peer_joined') as total_views,
               COUNT(*) FILTER (WHERE event_type = 'peer_left') as total_leaves,
               COALESCE(SUM(bytes_p2p), 0) as total_p2p,
               COALESCE(SUM(bytes_origin), 0) as total_origin
           FROM analytics WHERE file_id = $1""",
        file_id,
    )

    total_bytes = (result["total_p2p"] or 0) + (result["total_origin"] or 0)
    p2p_offload_percent = 0
    bandwidth_saved_gb = 0
    if total_bytes > 0:
        p2p_offload_percent = (result["total_p2p"] / total_bytes) * 100
        bandwidth_saved_gb = result["total_p2p"] / (1024 * 1024 * 1024)

    active_peers = (result["total_views"] or 0) - (result["total_leaves"] or 0)
    active_peers = max(active_peers, 0)

    return {
        "total_views": result["total_views"] or 0,
        "p2p_offload_percent": round(p2p_offload_percent, 1),
        "bandwidth_saved_gb": round(bandwidth_saved_gb, 3),
        "active_peers": active_peers,
        # Absolute byte volumes (GB) so consumers can compute costs directly
        # even when offload is 0% (origin-only traffic) — the percentage alone
        # cannot reconstruct the total once P2P shares hit zero.
        "total_gb": round(total_bytes / (1024 * 1024 * 1024), 3),
        "origin_gb": round((result["total_origin"] or 0) / (1024 * 1024 * 1024), 3),
    }


def _prune_analytics_event_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _analytics_event_requests.items() if not v or now - v[-1] > _ANALYTICS_EVENT_WINDOW]
    for k in expired:
        del _analytics_event_requests[k]
    enforce_max_size(_analytics_event_requests)


def _prune_analytics_file_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _analytics_file_requests.items() if not v or now - v[-1] > _ANALYTICS_FILE_WINDOW]
    for k in expired:
        del _analytics_file_requests[k]
    enforce_max_size(_analytics_file_requests)


async def _buffer_pressure() -> int:
    """Approximate number of events waiting to be written (both buffer kinds)."""
    redis = await get_redis()
    if redis is not None:
        try:
            return int(await redis.llen(ANALYTICS_BUFFER_KEY) or 0) + len(_memory_event_buffer)
        except Exception:
            pass
    return len(_memory_event_buffer)


async def _enqueue_event(record: dict, redis) -> None:
    """Append one validated event to the ingestion buffer.

    Redis LPUSH (the drainer RPOPs) when Redis is up — the buffer survives a
    process restart and is shared by all gunicorn workers. Redis down: an
    in-memory deque with the same semantics for this worker. Either way the
    request path never touches the DB.

    The cap is a SHED line, not a queue: past ANALYTICS_BUFFER_MAX the newest
    events are dropped (the buffer is already hours of drain time — more
    queueing only delays the DB collapse it exists to prevent).
    """
    global _memory_event_buffer
    payload = json.dumps(record, separators=(",", ":"))
    if redis is not None:
        try:
            length = await redis.llen(ANALYTICS_BUFFER_KEY)
            if length and int(length) >= ANALYTICS_BUFFER_MAX:
                return  # shed: buffer full
            await redis.lpush(ANALYTICS_BUFFER_KEY, payload)
            return
        except Exception as e:
            logger.warning("analytics Redis buffer unavailable, falling back to memory: %s", e)
    if len(_memory_event_buffer) >= ANALYTICS_BUFFER_MAX:
        return  # shed: buffer full
    _memory_event_buffer.append(payload)


async def _drain_analytics_buffer_once() -> int:
    """Pop up to ANALYTICS_BATCH_SIZE events and INSERT them in ONE statement.

    Returns the number of events written. Uses executemany-style multi-VALUES
    INSERT (single round-trip per batch). Redis errors mid-drain push the popped
    events BACK onto the front (LPUSH the exact payloads) so a Redis blip
    during a drain never loses the batch — the next drain retries it.
    """
    redis = await get_redis()
    items: list[str] = []
    if redis is not None:
        try:
            popped = await redis.rpop(ANALYTICS_BUFFER_KEY, count=ANALYTICS_BATCH_SIZE)
            items = list(popped or [])
        except Exception:
            items = []
    if not items and _memory_event_buffer:
        take = min(ANALYTICS_BATCH_SIZE, len(_memory_event_buffer))
        items = _memory_event_buffer[:take]
        del _memory_event_buffer[:take]
    if not items:
        return 0

    rows = []
    for raw in items:
        try:
            r = json.loads(raw)
            rows.append((UUID(r["file_id"]), r["event_type"], int(r["bytes_p2p"]), int(r["bytes_origin"])))
        except Exception:
            continue  # malformed payload — drop, never block the batch
    if not rows:
        return 0

    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO analytics (file_id, event_type, bytes_p2p, bytes_origin)
                   VALUES ($1, $2, $3, $4)""",
                rows,
            )
        return len(rows)
    except Exception as e:
        # DB error: push the batch back (Redis) / re-prepend (memory) so the
        # events are retried by the next drain instead of vanishing.
        logger.warning("analytics batch INSERT failed (%s) — requeueing %d events", e, len(rows))
        raw_rows = [raw for raw, r in zip(items, rows)] if len(rows) == len(items) else [
            json.dumps({"file_id": str(r[0]), "event_type": r[1], "bytes_p2p": r[2], "bytes_origin": r[3]}, separators=(",", ":"))
            for r in rows
        ]
        if redis is not None:
            try:
                await redis.lpush(ANALYTICS_BUFFER_KEY, *reversed(raw_rows))
                return 0
            except Exception:
                pass
        _memory_event_buffer[:0] = list(reversed(raw_rows))
        if len(_memory_event_buffer) > ANALYTICS_BUFFER_MAX:
            del _memory_event_buffer[ANALYTICS_BUFFER_MAX:]
        return 0


async def _analytics_flush_loop() -> None:
    """Background drainer: batch-INSERT buffered events every few seconds.

    Started once per process (lifespan startup). Drains the Redis buffer AND
    the in-memory fallback in the same loop so both fill paths share one DB
    writer per worker.
    """
    while True:
        try:
            await asyncio.sleep(ANALYTICS_FLUSH_INTERVAL)
            while True:
                written = await _drain_analytics_buffer_once()
                if written == 0:
                    break
        except asyncio.CancelledError:
            # Final drain on shutdown so buffered events aren't lost on deploy.
            try:
                while await _drain_analytics_buffer_once():
                    pass
            except Exception:
                pass
            raise
        except Exception as e:
            logger.warning("analytics flush loop error: %s", e)


def ensure_analytics_flusher(event_loop=None) -> None:
    """Start the background drainer exactly once per process (idempotent).

    Called from the app lifespan; tests exercise the drain function directly so
    they never depend on the loop.
    """
    global _buffer_task_started
    if _buffer_task_started:
        return
    _buffer_task_started = True
    asyncio.get_event_loop().create_task(_analytics_flush_loop())


def _prune_analytics_event_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _analytics_event_requests.items() if not v or now - v[-1] > _ANALYTICS_EVENT_WINDOW]
    for k in expired:
        del _analytics_event_requests[k]
    enforce_max_size(_analytics_event_requests)


@router.post("/event")
async def track_event(
    event: AnalyticsEventCreate,
    request: Request,
):
    """Track an analytics event. Public endpoint — no auth required."""
    # Validate event type
    if event.event_type not in VALID_EVENTS:
        raise HTTPException(status_code=400, detail="Invalid event type")

    # IP-based rate limiting (Redis primary, in-memory fallback when down).
    # This public write endpoint stays throttled even without Redis.
    ip = get_client_ip(request)
    redis = await get_redis()
    if redis is not None:
        rate_key = f"analytics_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= settings.rate_limit_analytics:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        await redis.incr(rate_key)
        await redis.expire(rate_key, 60)
    else:
        now = time.time()
        _prune_analytics_event_requests(now)
        recent = [t for t in _analytics_event_requests.get(ip, []) if now - t < _ANALYTICS_EVENT_WINDOW]
        if len(recent) >= settings.rate_limit_analytics:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        recent.append(now)
        _analytics_event_requests[ip] = recent

    # M1 FIX: Per-IP per-file rate limit — prevents inflating a specific file's
    # P2P offload percentage by submitting many small events from the same IP.
    # 30 events/min/file/IP is generous for legitimate embed players while
    # blocking automated inflation scripts.
    _PER_FILE_LIMIT = 30
    if redis is not None:
        file_rate_key = f"analytics_file:{ip}:{event.file_id}"
        file_count = await redis.get(file_rate_key)
        if file_count and int(file_count) >= _PER_FILE_LIMIT:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        await redis.incr(file_rate_key)
        await redis.expire(file_rate_key, 60)
    else:
        # Redis-down twin of the Redis per-file key: without this the per-file
        # cap silently disappears during a Redis outage and the IP-level budget
        # (600/min) becomes the only bound on one file's inflation.
        now = time.time()
        _prune_analytics_file_requests(now)
        file_key = f"{ip}:{event.file_id}"
        recent_file = [t for t in _analytics_file_requests.get(file_key, []) if now - t < _ANALYTICS_FILE_WINDOW]
        if len(recent_file) >= _PER_FILE_LIMIT:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        recent_file.append(now)
        _analytics_file_requests[file_key] = recent_file

    # Per-file size cap WITHOUT a DB round-trip: file_size_mb is immutable
    # after upload, so it caches perfectly (60s TTL, written by the same Redis
    # the buffer uses). On a miss (cold cache / Redis down) fall back to the
    # uploads SELECT exactly as before — correctness first, speed second.
    # At 200k viewers this turns ~200k SELECTs/min into ~0 (the cache hit rate
    # for an actively-viewed file is ~100%).
    file_size_mb: float | None = None
    if redis is not None:
        try:
            cached = await redis.get(f"analytics:file_size:{event.file_id}")
            if cached is not None:
                file_size_mb = float(cached)
        except Exception:
            file_size_mb = None
    if file_size_mb is None:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, file_size_mb FROM uploads WHERE id = $1", event.file_id
            )
        if not row:
            raise HTTPException(status_code=404, detail="File not found")
        file_size_mb = float(row["file_size_mb"] or 0)
        if redis is not None:
            try:
                await redis.setex(
                    f"analytics:file_size:{event.file_id}", 60, str(file_size_mb)
                )
            except Exception:
                pass  # cache write is best-effort — the lookup already succeeded

    # File-size-relative sanity bound on the byte counters (see the module
    # constants): the aggregate queries SUM the two columns, so the bound
    # is on the SUM — an event reporting more than 10x the file's size
    # total is a forged/tampered counter, not a real delivery report
    # (legit clients report P2P OR origin bytes per event, never both at
    # scale). Rejected rather than clamped so the attempt is visible (400)
    # instead of silently contributing its maximum.
    file_bytes = file_size_mb * 1024 * 1024
    max_event_bytes = max(
        file_bytes * ANALYTICS_EVENT_BYTES_MULTIPLIER,
        ANALYTICS_EVENT_BYTES_FLOOR,
    )
    if event.bytes_p2p + event.bytes_origin > max_event_bytes:
        raise HTTPException(
            status_code=400,
            detail="Byte counters exceed the file's size (possible analytics tampering)",
        )

    # Load-shedding gate BEFORE the enqueue: past the buffer threshold,
    # chunk_downloaded events are admitted only at 1-in-N. The aggregate
    # becomes a representative byte sample; views/leaves stay exact. A 202
    # (not 200) tells a batch-aware client the event was sampled — the SDK
    # treats any 2xx as delivered.
    if (
        event.event_type == "chunk_downloaded"
        and await _buffer_pressure() >= ANALYTICS_SAMPLE_THRESHOLD
        and (time.monotonic_ns() % ANALYTICS_SAMPLE_N) != 0
    ):
        return JSONResponse(status_code=202, content={"detail": "Event sampled"})

    # Buffer append — O(1), no DB on the hot path. The background drainer
    # flushes ANALYTICS_BATCH_SIZE-sized INSERTs every ANALYTICS_FLUSH_INTERVAL.
    await _enqueue_event(
        {
            "file_id": str(event.file_id),
            "event_type": event.event_type,
            "bytes_p2p": event.bytes_p2p,
            "bytes_origin": event.bytes_origin,
        },
        redis,
    )

    return {"detail": "Event tracked"}


@router.get("/bandwidth-history")
async def get_bandwidth_history(user_id: UUID = Depends(get_current_user_id)):
    """Daily origin bandwidth (GB) for the current user over the last 7 days.

    Queries chunk_downloaded analytics joined to the user's own uploads, then
    zero-fills missing days so the chart always has exactly 7 points.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                    DATE(a.created_at) as date,
                    COALESCE(SUM(a.bytes_origin) / (1024*1024*1024.0), 0) as gb_used
               FROM analytics a
               JOIN uploads u ON a.file_id = u.id
              WHERE u.user_id = $1
                AND a.created_at >= NOW() - INTERVAL '7 days'
                AND a.event_type = 'chunk_downloaded'
              GROUP BY DATE(a.created_at)
              ORDER BY date ASC""",
            user_id,
        )

    by_date = {r["date"]: r["gb_used"] for r in rows}
    today = datetime.now(UTC).date()
    return [
        {
            "date": (today - timedelta(days=i)).isoformat(),
            "gb": round(by_date.get(today - timedelta(days=i), 0), 4),
        }
        for i in range(6, -1, -1)
    ]


@router.get("/public/{file_id}")
async def get_public_analytics(file_id: UUID, request: Request):
    """Public delivery stats for the demo/embed pages — no auth required.

    Exposes only aggregate, low-sensitivity delivery stats (view count, P2P
    offload %, bandwidth saved, active sessions) — never filenames, owner
    identity, or storage URLs. Same payload shape as the authed endpoint so
    the frontend reuses one client interface.
    """
    # Rate limit: 120 requests per minute per IP (Redis primary, in-memory fallback)
    ip = get_client_ip(request)
    redis = await get_redis()
    if redis is not None:
        rate_key = f"analytics_get_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= ANALYTICS_GET_LIMIT:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        await redis.incr(rate_key)
        await redis.expire(rate_key, 60)
    else:
        now = time.time()
        _prune_analytics_get_requests(now)
        recent = [t for t in _analytics_get_requests.get(ip, []) if now - t < _ANALYTICS_GET_WINDOW]
        if len(recent) >= ANALYTICS_GET_LIMIT:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        recent.append(now)
        _analytics_get_requests[ip] = recent

    # Read cache (5s TTL): the demo/embed page polls this every 5s per viewer,
    # so 200k viewers = 40k polls/min. Each poll previously recomputed a full
    # SUM/COUNT over the file's whole analytics history — an unbounded scan
    # that got slower as the table grew. A 5s cache bounds the DB to ONE
    # aggregate query per file per 5s (12/min) no matter how many viewers
    # poll; a 5s-stale stat is indistinguishable in the UI.
    read_cache_key = f"analytics:public_agg:{file_id}"
    cache_headers = {"Cache-Control": f"public, max-age=5, stale-while-revalidate=5"}
    if redis is not None:
        try:
            cached = await redis.get(read_cache_key)
            if cached:
                return JSONResponse(json.loads(cached), headers=cache_headers)
        except Exception:
            pass

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        file_exists = await conn.fetchrow(
            "SELECT id FROM uploads WHERE id = $1", file_id,
        )
        if not file_exists:
            raise HTTPException(status_code=404, detail="File not found")

        result = await _aggregate_file_analytics(conn, file_id)

    if redis is not None:
        try:
            await redis.setex(read_cache_key, 5, json.dumps(result))
        except Exception:
            pass  # cache write is best-effort — the DB read already succeeded
    return JSONResponse(result, headers=cache_headers)


@router.get("/{file_id}")
async def get_analytics(
    file_id: UUID,
    request: Request,
    user_id: UUID = Depends(get_current_user_id),
):
    """Get analytics for a file. Auth required, ownership-scoped.

    Scoped to the caller's own uploads so the endpoint can't be used as a
    cross-account file-existence oracle or to read another user's stats.
    """
    # Rate limit: 120 requests per minute per IP (Redis primary, in-memory fallback)
    ip = get_client_ip(request)
    redis = await get_redis()
    if redis is not None:
        rate_key = f"analytics_get_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= ANALYTICS_GET_LIMIT:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        await redis.incr(rate_key)
        await redis.expire(rate_key, 60)
    else:
        now = time.time()
        _prune_analytics_get_requests(now)
        recent = [t for t in _analytics_get_requests.get(ip, []) if now - t < _ANALYTICS_GET_WINDOW]
        if len(recent) >= ANALYTICS_GET_LIMIT:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        recent.append(now)
        _analytics_get_requests[ip] = recent

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Ownership check — same 404 for missing and not-owned (no oracle).
        file_exists = await conn.fetchrow(
            "SELECT id FROM uploads WHERE id = $1 AND user_id = $2",
            file_id, user_id,
        )
        if not file_exists:
            raise HTTPException(status_code=404, detail="File not found")

        return await _aggregate_file_analytics(conn, file_id)


# 5s TTL for the public aggregate cache (see get_public_analytics).
ANALYTICS_AGG_CACHE_TTL = 5
