"""Live streaming REST API — channels, keys, status, analytics, playback.

Modular by design: this module never modifies VOD behavior; it reuses the
existing auth (get_current_user_id), database (asyncpg + get_db_pool), Redis
(get_redis), and rate-limit patterns. Channel status is DERIVED from actual
stream activity (MediaMTX paths + the segment registry), never from a
manually-set status field.

Tenancy: every channel is scoped by owner_id/tenant_id; cross-tenant access
gets the same 404 as a missing channel (no existence oracle).

Endpoints are mounted under the /api prefix in production (Vercel rewrite),
matching the existing FastAPI route roots (auth /files /analytics …).
"""
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import get_current_user_id
from ..database import get_db_pool, get_redis
from ..live_models import (
    LiveChannelCreate,
    LiveChannelOut,
    LiveChannelRotateResponse,
    LiveChannelUpdate,
    LiveHeartbeat,
    LivePlaybackInfo,
    LiveSegmentAvailability,
    LiveSessionCreate,
    LiveSessionOut,
    MAX_SLUG_LEN,
    hash_stream_key,
    new_slug,
    new_stream_key,
    stream_key_prefix,
)
from ..live_settings import get_live_settings
from ..live_state import (
    active_peers,
    active_viewers,
    bump_counters,
    clear_channel,
    get_channel_meta,
    latest_segment,
    peers_with_segment,
    read_counters,
)
from ..mediamtx import (
    get_path_by_ingress,
    hls_url,
    mint_playback_token,
    verify_playback_token,
)
from ..security import get_client_ip
from ..security_events import record as record_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/live", tags=["live"])

LIVE_ANALYTICS_WINDOW_HOURS = 24

# In-memory fallback for the viewer-token rate limiter (Redis down) — same
# bounded pattern as the analytics/files limiters.
_token_requests: dict[str, list[float]] = {}
_TOKEN_WINDOW = 60


def _prune_token_requests(now: float) -> None:
    expired = [k for k, v in _token_requests.items() if not v or now - v[-1] > _TOKEN_WINDOW]
    for k in expired:
        del _token_requests[k]
    from ..security import enforce_max_size
    enforce_max_size(_token_requests)


async def _check_viewer_rate_limit(request: Request, channel_slug: str) -> None:
    """Per-IP-per-channel budget for the unauthenticated-ish viewer endpoints
    (session join / heartbeat / availability, gated by playback tokens)."""
    ip = get_client_ip(request)
    limit = get_live_settings().token_rate_limit
    redis = await get_redis()
    if redis is not None:
        key = f"live_token_rate:{ip}:{channel_slug}"
        count = await redis.get(key)
        if count and int(count) >= limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        await redis.incr(key)
        await redis.expire(key, _TOKEN_WINDOW)
        return
    now = time.time()
    _prune_token_requests(now)
    key = f"{ip}:{channel_slug}"
    recent = [t for t in _token_requests.get(key, []) if now - t < _TOKEN_WINDOW]
    if len(recent) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    recent.append(now)
    _token_requests[key] = recent


# ── DB helpers ───────────────────────────────────────────────────────────────

def _row_to_out(row) -> LiveChannelOut:
    return LiveChannelOut(
        id=row["id"],
        tenant_id=row["tenant_id"],
        owner_id=row["owner_id"],
        name=row["name"],
        slug=row["slug"],
        description=row.get("description"),
        status=row.get("status") or "OFFLINE",
        enabled=bool(row.get("enabled", True)),
        stream_key_prefix=row.get("stream_key_prefix"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_seen_at=row.get("last_seen_at"),
    )


# Full SQL literals — the repo's static SQL-injection audit requires plain
# string literals with $n placeholders (asyncpg parameterization), never
# f-string SQL. The shared column list is spelled out in every statement.
_CHANNEL_SELECT = """SELECT id, tenant_id, owner_id, name, slug, description, stream_key_hash,
    stream_key_prefix, status, enabled, created_at, updated_at, last_seen_at
    FROM live_channels"""

_CHANNEL_SELECT_WHERE_OWNER = """SELECT id, tenant_id, owner_id, name, slug, description, stream_key_hash,
    stream_key_prefix, status, enabled, created_at, updated_at, last_seen_at
    FROM live_channels WHERE id = $1 AND owner_id = $2"""

_CHANNEL_SELECT_WHERE_SLUG = """SELECT id, tenant_id, owner_id, name, slug, description, stream_key_hash,
    stream_key_prefix, status, enabled, created_at, updated_at, last_seen_at
    FROM live_channels WHERE slug = $1"""

_CHANNEL_SELECT_WHERE_OWNER_SLUG = """SELECT id, tenant_id, owner_id, name, slug, description, stream_key_hash,
    stream_key_prefix, status, enabled, created_at, updated_at, last_seen_at
    FROM live_channels WHERE slug = $1 AND owner_id = $2"""

_CHANNEL_SELECT_WHERE_SLUG_ORDER = """SELECT id, tenant_id, owner_id, name, slug, description, stream_key_hash,
    stream_key_prefix, status, enabled, created_at, updated_at, last_seen_at
    FROM live_channels WHERE owner_id = $1 ORDER BY created_at DESC"""


async def _get_channel_scoped(conn, channel_id: UUID, user_id: UUID):
    """Fetch a channel enforcing tenant/owner scope — same-404, no oracle."""
    return await conn.fetchrow(_CHANNEL_SELECT_WHERE_OWNER, channel_id, user_id)


async def _get_channel_by_slug(conn, slug: str):
    return await conn.fetchrow(_CHANNEL_SELECT_WHERE_SLUG, slug)


async def require_channel_owner(channel_id: UUID, user_id: UUID):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await _get_channel_scoped(conn, channel_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")
    return row


# ── Status derivation (from actual MediaMTX/stream activity) ─────────────────

async def derive_channel_status(row) -> str:
    """Derive OFFLINE/STARTING/LIVE/ERROR from real activity.

    Precedence:
    1. disabled channel → OFFLINE (no playback, no publish)
    2. MediaMTX reports the path ready → LIVE (authoritative)
    3. a recent segment exists in the registry (fresh within the max-age)
       → LIVE; a slightly-older one → STARTING (muxing window)
    4. last_seen very recent but no MediaMTX answer → STARTING (MediaMTX
       unreachable — never invent LIVE)
    5. otherwise OFFLINE
    """
    if not row.get("enabled", True):
        return "OFFLINE"
    s = get_live_settings()
    slug = row["slug"]

    path = await get_path_by_ingress(slug)
    if path and path.get("ready"):
        return "LIVE"

    meta = await get_channel_meta(slug)
    last_seg_ts = float(meta.get("last_seg_ts", 0) or 0)
    if last_seg_ts:
        age = time.time() - last_seg_ts
        if age <= s.live_max_segment_age:
            return "LIVE"
        if age <= s.live_max_segment_age * 3:
            return "STARTING"

    last_seen = row.get("last_seen_at")
    if last_seen:
        try:
            seen_age = (datetime.now(UTC) - last_seen).total_seconds()
            if seen_age < s.live_max_segment_age:
                return "STARTING"
        except (TypeError, ValueError):
            pass
    return "OFFLINE"


async def monitor_channel_status(slug: str) -> None:
    """Monitor-tick hook: refresh status + last_seen for every channel with
    the given slug (normally one; the slug is globally unique per tenant)."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_CHANNEL_SELECT_WHERE_SLUG, slug)
        for row in rows:
            status = await derive_channel_status(row)
            if status != (row.get("status") or "OFFLINE"):
                await conn.execute(
                    "UPDATE live_channels SET status = $1, updated_at = NOW() WHERE id = $2",
                    status, row["id"],
                )
            if status == "LIVE":
                await conn.execute(
                    "UPDATE live_channels SET last_seen_at = NOW() WHERE id = $2 AND (last_seen_at IS NULL OR last_seen_at < NOW() - INTERVAL '10 seconds')",
                    row["id"],
                )


async def rollup_channel_analytics(slug: str) -> None:
    """Fold Redis rolling counters into a durable live_analytics_events row.

    A row is written when the counters hold anything meaningful AND the
    channel has been quiet for a rollup window (stream end / lull). Resets
    the counters so each row covers one interval. Historical analytics are
    preserved forever — only ephemeral state expires.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM live_channels WHERE slug = $1", slug
        )
        if not row:
            return
    counters = await read_counters(slug)
    meta = await get_channel_meta(slug)
    if not counters and not meta:
        return
    total_bytes = int(counters.get("bytes_p2p", 0)) + int(counters.get("bytes_origin", 0))
    last_activity = float(meta.get("last_seg_ts", 0) or 0)
    now = time.time()
    has_data = total_bytes > 0 or int(counters.get("segments_served", 0)) > 0
    quiet = (now - last_activity) > 60 if last_activity else False
    interval_elapsed = now - float(meta.get("last_rollup_ts", 0) or 0) > 300
    if not has_data or not (quiet or interval_elapsed):
        return
    channel_id = row["id"]
    await pool_execute_insert_analytics(pool, channel_id, counters, meta)
    await bump_counters(slug, **{k: -int(v) for k, v in counters.items() if v})
    await set_meta_rollup(slug, now)


async def pool_execute_insert_analytics(pool, channel_id, counters, meta) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO live_analytics_events
               (channel_id, started_at, duration_seconds, peak_viewers, peak_peers,
                bytes_p2p, bytes_origin, segments_served, segments_failed,
                buffering_events, playback_errors, avg_startup_ms, metadata)
               VALUES ($1, NOW() - ($2 || ' seconds')::interval, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)""",
            channel_id,
            int(counters.get("interval_seconds", 0) or 0),
            int(counters.get("peak_viewers", 0) or 0),
            int(counters.get("peak_peers", 0) or 0),
            int(counters.get("bytes_p2p", 0) or 0),
            int(counters.get("bytes_origin", 0) or 0),
            int(counters.get("segments_served", 0) or 0),
            int(counters.get("segments_failed", 0) or 0),
            int(counters.get("buffering_events", 0) or 0),
            int(counters.get("playback_errors", 0) or 0),
            int(counters.get("avg_startup_ms", 0) or 0),
            json.dumps({"slug": meta.get("slug", ""), "source": "monitor_rollup"}),
        )


async def set_meta_rollup(slug: str, ts: float) -> None:
    from ..live_state import set_channel_meta
    await set_channel_meta(slug, last_rollup_ts=str(ts))


# ── Channel CRUD ─────────────────────────────────────────────────────────────

@router.post("/channels", response_model=LiveChannelOut, status_code=201)
async def create_channel(
    data: LiveChannelCreate,
    user_id: UUID = Depends(get_current_user_id),
):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetch(
            "SELECT slug FROM live_channels WHERE tenant_id = $1", user_id
        )
        slug = data.slug or new_slug(data.name, {r["slug"] for r in existing})
        slug = slug[:MAX_SLUG_LEN]
        key = new_stream_key()
        row = await conn.fetchrow(
            """INSERT INTO live_channels (tenant_id, owner_id, name, slug, description, stream_key_hash, stream_key_prefix, status)
               VALUES ($1, $1, $2, $3, $4, $5, $6, 'OFFLINE')
               RETURNING id, tenant_id, owner_id, name, slug, description, stream_key_prefix, status, enabled, created_at, updated_at, last_seen_at""",
            user_id, data.name, slug, data.description, hash_stream_key(key), stream_key_prefix(key),
        )
    record_event("live_channel_created", detail=f"channel {slug}")
    out = _row_to_out(row)
    # The plaintext key is shown exactly once, at creation — same UX as API keys.
    out.stream_key_prefix = stream_key_prefix(key)
    return out


@router.get("/channels", response_model=list[LiveChannelOut])
async def list_channels(user_id: UUID = Depends(get_current_user_id)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _CHANNEL_SELECT_WHERE_SLUG_ORDER,
            user_id,
        )
    out = []
    for row in rows:
        ch = _row_to_out(row)
        await _attach_live_metrics(ch)
        out.append(ch)
    return out


async def _attach_live_metrics(ch: LiveChannelOut) -> None:
    meta = await get_channel_meta(ch.slug)
    try:
        ch.latest_segment_sequence = int(meta.get("latest_seq", -1)) if meta.get("latest_seq") else None
    except (TypeError, ValueError):
        ch.latest_segment_sequence = None
    counters = await read_counters(ch.slug)
    ch.current_viewers = await active_viewers(ch.slug)
    ch.current_peers = len(await active_peers(ch.slug))
    total = counters.get("bytes_p2p", 0) + counters.get("bytes_origin", 0)
    # bitrate from the rolling counters (bytes in the last counter window)
    ch.bitrate_kbps = round(total * 8 / 1000 / max(60, 1), 1) if total else 0.0
    if ch.status == "LIVE" and ch.last_seen_at:
        try:
            ch.uptime_seconds = max(0, int((datetime.now(UTC) - ch.last_seen_at).total_seconds()))
        except (TypeError, ValueError):
            ch.uptime_seconds = 0


@router.get("/channels/{channel_id}", response_model=LiveChannelOut)
async def get_channel(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    row = await require_channel_owner(channel_id, user_id)
    ch = _row_to_out(row)
    await _attach_live_metrics(ch)
    return ch


@router.patch("/channels/{channel_id}", response_model=LiveChannelOut)
async def update_channel(
    channel_id: UUID,
    data: LiveChannelUpdate,
    user_id: UUID = Depends(get_current_user_id),
):
    row = await require_channel_owner(channel_id, user_id)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        updated = await conn.fetchrow(
            """UPDATE live_channels SET
                 name = COALESCE($2, name),
                 description = COALESCE($3, description),
                 updated_at = NOW()
               WHERE id = $1
               RETURNING id, tenant_id, owner_id, name, slug, description, stream_key_prefix, status, enabled, created_at, updated_at, last_seen_at""",
            channel_id, data.name, data.description,
        )
    if not updated:
        raise HTTPException(status_code=404, detail="Channel not found")
    ch = _row_to_out(updated)
    await _attach_live_metrics(ch)
    return ch


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    row = await require_channel_owner(channel_id, user_id)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM live_channels WHERE id = $1", channel_id)
    await clear_channel(row["slug"])
    record_event("live_channel_deleted", detail=f"channel {row['slug']}")
    return {"detail": "Channel deleted"}


@router.post("/channels/{channel_id}/disable")
async def disable_channel(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    return await _set_enabled(channel_id, user_id, False)


@router.post("/channels/{channel_id}/enable")
async def enable_channel(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    return await _set_enabled(channel_id, user_id, True)


async def _set_enabled(channel_id: UUID, user_id: UUID, enabled: bool) -> dict:
    await require_channel_owner(channel_id, user_id)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE live_channels SET enabled = $2, updated_at = NOW() WHERE id = $1",
            channel_id, enabled,
        )
    return {"detail": f"Channel {'enabled' if enabled else 'disabled'}"}


# ── Stream keys ──────────────────────────────────────────────────────────────

@router.post("/channels/{channel_id}/rotate-key", response_model=LiveChannelRotateResponse)
async def rotate_key(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    """Regenerate the stream key — the old key stops working immediately
    (hash replaced; MediaMTX auth consults this hash on every publish)."""
    await require_channel_owner(channel_id, user_id)
    key = new_stream_key()
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE live_channels SET stream_key_hash = $2, stream_key_prefix = $3, updated_at = NOW() WHERE id = $1",
            channel_id, hash_stream_key(key), stream_key_prefix(key),
        )
    record_event("live_key_rotated", detail=f"channel {channel_id}")
    return LiveChannelRotateResponse(
        channel_id=channel_id, stream_key=key, stream_key_prefix=stream_key_prefix(key)
    )


# ── Status + analytics ───────────────────────────────────────────────────────

@router.get("/channels/{channel_id}/status")
async def channel_status(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    row = await require_channel_owner(channel_id, user_id)
    status = await derive_channel_status(row)
    s = get_live_settings()
    slug = row["slug"]
    meta = await get_channel_meta(slug)
    latest = await latest_segment(slug)
    viewers = await active_viewers(slug)
    peers = await active_peers(slug)
    counters = await read_counters(slug)
    total = counters.get("bytes_p2p", 0) + counters.get("bytes_origin", 0)
    return {
        "channel_id": str(channel_id),
        "slug": slug,
        "status": status,
        "enabled": bool(row.get("enabled", True)),
        "current_viewers": viewers,
        "current_peers": len(peers),
        "bitrate_kbps": round(total * 8 / 1000 / max(60, 1), 1) if total else 0.0,
        "uptime_seconds": _uptime(row, status),
        "latest_segment_sequence": latest,
        "latency_target_seconds": s.hls_segment_duration,
        "last_seen_at": row.get("last_seen_at").isoformat() if row.get("last_seen_at") else None,
    }


def _uptime(row, status: str) -> int:
    if status != "LIVE" or not row.get("last_seen_at"):
        return 0
    try:
        return max(0, int((datetime.now(UTC) - row["last_seen_at"]).total_seconds()))
    except (TypeError, ValueError):
        return 0


@router.get("/channels/{channel_id}/analytics")
async def channel_analytics(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    hours: int = Query(default=24, ge=1, le=168),
):
    row = await require_channel_owner(channel_id, user_id)
    slug = row["slug"]
    since = datetime.now(UTC) - timedelta(hours=hours)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        agg = await conn.fetchrow(
            """SELECT
                 COALESCE(SUM(bytes_p2p), 0)::bigint AS bytes_p2p,
                 COALESCE(SUM(bytes_origin), 0)::bigint AS bytes_origin,
                 COALESCE(SUM(segments_served), 0)::bigint AS segments_served,
                 COALESCE(SUM(segments_failed), 0)::bigint AS segments_failed,
                 COALESCE(SUM(buffering_events), 0)::bigint AS buffering_events,
                 COALESCE(SUM(playback_errors), 0)::bigint AS playback_errors,
                 COALESCE(SUM(duration_seconds), 0)::float AS uptime_seconds,
                 COALESCE(MAX(peak_viewers), 0)::int AS peak_viewers,
                 COALESCE(MAX(peak_peers), 0)::int AS peak_peers,
                 COALESCE(AVG(NULLIF(avg_startup_ms, 0)), 0)::float AS avg_startup_ms,
                 COUNT(*)::int AS sessions
               FROM live_analytics_events
               WHERE channel_id = $1 AND created_at >= $2""",
            channel_id, since,
        )
        recent = await conn.fetch(
            """SELECT created_at, bytes_p2p, bytes_origin, peak_viewers, peak_peers,
                      buffering_events, playback_errors, duration_seconds
               FROM live_analytics_events
               WHERE channel_id = $1 AND created_at >= $2
               ORDER BY created_at DESC LIMIT 100""",
            channel_id, since,
        )
    counters = await read_counters(slug)
    # Live (unrolled) counters count toward the current window.
    bytes_p2p = int(agg["bytes_p2p"] or 0) + counters.get("bytes_p2p", 0)
    bytes_origin = int(agg["bytes_origin"] or 0) + counters.get("bytes_origin", 0)
    total = bytes_p2p + bytes_origin
    segments_served = int(agg["segments_served"] or 0) + counters.get("segments_served", 0)
    segments_failed = int(agg["segments_failed"] or 0) + counters.get("segments_failed", 0)
    transfers = segments_served + segments_failed
    return {
        "channel_id": str(channel_id),
        "window_hours": hours,
        "bytes_p2p": bytes_p2p,
        "bytes_origin": bytes_origin,
        "total_bytes": total,
        "p2p_offload_percent": round(100.0 * bytes_p2p / total, 1) if total else 0.0,
        "segments_served": segments_served,
        "segments_failed": segments_failed,
        "transfer_success_rate": round(100.0 * segments_served / transfers, 1) if transfers else 0.0,
        "transfer_failure_rate": round(100.0 * segments_failed / transfers, 1) if transfers else 0.0,
        "buffering_events": int(agg["buffering_events"] or 0) + counters.get("buffering_events", 0),
        "playback_errors": int(agg["playback_errors"] or 0) + counters.get("playback_errors", 0),
        "peak_viewers": int(agg["peak_viewers"] or 0),
        "peak_peers": int(agg["peak_peers"] or 0),
        "avg_startup_ms": round(float(agg["avg_startup_ms"] or 0)),
        "uptime_seconds": round(float(agg["uptime_seconds"] or 0)),
        "sessions": int(agg["sessions"] or 0),
        "current": {
            "viewers": await active_viewers(slug),
            "peers": len(await active_peers(slug)),
        },
    }


@router.get("/channels/{channel_id}/segments")
async def channel_segments(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    """Segment availability map (owner-only): which peers hold which segment
    in the live window + origin availability."""
    row = await require_channel_owner(channel_id, user_id)
    slug = row["slug"]
    latest = await latest_segment(slug)
    if latest is None:
        return {"slug": slug, "segments": []}
    meta = await get_channel_meta(slug)
    try:
        first = max(0, latest - 10)
    except (TypeError, ValueError):
        first = 0
    out = []
    for seq in range(first, latest + 1):
        peers = await peers_with_segment(slug, seq)
        out.append({
            "sequence_number": seq,
            "peers": peers,
            "origin": True,  # origin holds the whole live window by definition
            "available": True,
        })
    return {
        "slug": slug,
        "latest_sequence": latest,
        "bitrate_kbps": float(meta.get("bitrate", 0) or 0),
        "segments": out,
    }


# ── Playback (viewer-facing, token-gated) ────────────────────────────────────

async def _resolve_channel_for_playback(conn, slug: str):
    return await conn.fetchrow(_CHANNEL_SELECT_WHERE_SLUG, slug)


@router.get("/channels/{channel_id}/playback", response_model=LivePlaybackInfo)
async def channel_playback(
    channel_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    """Owner dashboard playback info (mints a fresh playback token)."""
    row = await require_channel_owner(channel_id, user_id)
    return await _playback_info(row)


@router.get("/play/{slug}", response_model=LivePlaybackInfo)
async def public_playback(
    slug: str,
    request: Request,
):
    """Public playback info by slug for enabled channels.

    Public channels (enabled=TRUE) play openly — the HLS URL contains no
    secrets and availability state carries no PII. Disabled channels are
    unplayable; a guessed slug cannot access a disabled/private channel's
    state (404, same as a missing channel).
    """
    await _check_viewer_rate_limit(request, slug)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await _resolve_channel_for_playback(conn, slug)
    if not row or not row.get("enabled", True):
        raise HTTPException(status_code=404, detail="Channel not found")
    return await _playback_info(row)


async def _playback_info(row) -> LivePlaybackInfo:
    s = get_live_settings()
    slug = row["slug"]
    status = await derive_channel_status(row)
    token = mint_playback_token(str(row["id"]), ttl_seconds=600)
    return LivePlaybackInfo(
        channel_id=row["id"],
        channel_slug=slug,
        channel_name=row["name"],
        status=status,
        hls_url=hls_url(slug),
        token=token,
        swarm_base=f"tcdnlive:{slug}",
        latency_target_seconds=s.hls_segment_duration,
    )


@router.post("/token")
async def mint_viewer_token(
    request: Request,
    body: dict,
):
    """Exchange owner credentials for a viewer token (embed/SDK use).

    Body: {channel_slug, api_key} — the api_key is the account's tcdn_ key;
    ownership of ANY channel in the account grants playback tokens (the
    account is the tenant).
    """
    slug = str(body.get("channel_slug") or "")[:MAX_SLUG_LEN]
    api_key = str(body.get("api_key") or "")
    if not slug or not api_key:
        raise HTTPException(status_code=400, detail="channel_slug and api_key required")
    await _check_viewer_rate_limit(request, slug)
    from ..auth import _resolve_api_key_user

    user_id = await _resolve_api_key_user(api_key)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _CHANNEL_SELECT_WHERE_OWNER_SLUG, slug, user_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"token": mint_playback_token(str(row["id"]), ttl_seconds=600)}


# ── Live sessions (viewer join / heartbeat / leave) ──────────────────────────

@router.post("/session", response_model=LiveSessionOut)
async def join_session(
    data: LiveSessionCreate,
    request: Request,
):
    """Viewer join. The playback token (issued by /play or /playback) must be
    presented as a Bearer token — a guessed slug without a token cannot open
    a session on a private channel. Returns the swarm + live-edge info."""
    await _check_viewer_rate_limit(request, data.channel_slug)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await _resolve_channel_for_playback(conn, data.channel_slug)
        if not row or not row.get("enabled", True):
            raise HTTPException(status_code=404, detail="Channel not found")
        auth_ok = _request_token_valid(request, str(row["id"]))
        if not auth_ok:
            raise HTTPException(status_code=401, detail="Playback token required")
        s = get_live_settings()
        await conn.execute(
            """INSERT INTO live_sessions (channel_id, session_id, peer_id, user_agent, region, last_heartbeat)
               VALUES ($1, $2, $3, $4, $5, NOW())
               ON CONFLICT (channel_id, session_id)
               DO UPDATE SET last_heartbeat = NOW(), ended_at = NULL, peer_id = COALESCE(EXCLUDED.peer_id, live_sessions.peer_id)""",
            row["id"], data.session_id, data.session_id, data.user_agent, data.region,
        )
    status = await derive_channel_status(row)
    latest = await latest_segment(data.channel_slug)
    live = status == "LIVE"
    return LiveSessionOut(
        session_id=data.session_id,
        channel_slug=data.channel_slug,
        live=live,
        latest_sequence=latest,
        live_edge_sequence=latest if live else None,
        latency_target_seconds=s.hls_segment_duration,
        swarm_base=f"tcdnlive:{data.channel_slug}",
    )


def _request_token_valid(request: Request, channel_id: str) -> bool:
    """Bearer playback-token check for viewer endpoints (stateless HMAC)."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    token = auth.split(" ", 1)[1].strip()
    return verify_playback_token(token, channel_id)


@router.post("/heartbeat")
async def heartbeat(
    data: LiveHeartbeat,
    request: Request,
):
    """Viewer heartbeat: presence + counters (upload/download bytes, segments
    served/failed, buffering, errors, current segment). Presence entries
    carry short TTLs — a viewer that stops sending simply expires."""
    await _check_viewer_rate_limit(request, data.channel_slug)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await _resolve_channel_for_playback(conn, data.channel_slug)
        if not row or not row.get("enabled", True):
            raise HTTPException(status_code=404, detail="Channel not found")
        if not _request_token_valid(request, str(row["id"])):
            raise HTTPException(status_code=401, detail="Playback token required")
        await conn.execute(
            """UPDATE live_sessions SET last_heartbeat = NOW()
               WHERE channel_id = $1 AND session_id = $2 AND ended_at IS NULL""",
            row["id"], data.session_id,
        )
    await bump_counters(
        data.channel_slug,
        bytes_p2p=data.bytes_uploaded,
        bytes_origin=data.bytes_downloaded,
        segments_served=data.segments_served,
        segments_failed=data.segments_failed,
        buffering_events=data.buffering_events,
        playback_errors=data.playback_errors,
    )
    await register_segment_availability(
        data.channel_slug, data.session_id, data.peer_id or data.session_id,
        haves=[data.current_segment] if data.current_segment is not None else [],
    )
    return {"ok": True}


@router.post("/leave")
async def leave_session(
    request: Request,
    body: dict,
):
    """Viewer leave: end the session row + drop presence immediately."""
    slug = str(body.get("channel_slug") or "")[:MAX_SLUG_LEN]
    session_id = str(body.get("session_id") or "")[:128]
    await _check_viewer_rate_limit(request, slug)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await _resolve_channel_for_playback(conn, slug)
        if not row or not row.get("enabled", True):
            raise HTTPException(status_code=404, detail="Channel not found")
        if not _request_token_valid(request, str(row["id"])):
            raise HTTPException(status_code=401, detail="Playback token required")
        await conn.execute(
            """UPDATE live_sessions SET ended_at = NOW()
               WHERE channel_id = $1 AND session_id = $2 AND ended_at IS NULL""",
            row["id"], session_id,
        )
    from ..live_state import mark_peer_dead
    await mark_peer_dead(slug, session_id)
    return {"ok": True}


# ── Segment availability (P2P transfer reporting) ────────────────────────────

async def register_segment_availability(slug: str, session_id: str, peer_id: str,
                                        haves: list[int]) -> None:
    await _register_availability_state(slug, peer_id, session_id, haves)


async def _register_availability_state(slug: str, peer_id: str, session_id: str,
                                       haves: list[int]) -> None:
    from ..live_state import peer_report
    await peer_report(slug, peer_id, session_id, haves)


@router.post("/segment/availability")
async def segment_availability(
    data: LiveSegmentAvailability,
    request: Request,
):
    """Viewer availability report: what this peer holds (haves) + measured
    transfer outcomes (served/failed per segment). Failed transfers scrub the
    reporting peer's availability so dead peers stop being selected."""
    await _check_viewer_rate_limit(request, data.channel_slug)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await _resolve_channel_for_playback(conn, data.channel_slug)
        if not row or not row.get("enabled", True):
            raise HTTPException(status_code=404, detail="Channel not found")
        if not _request_token_valid(request, str(row["id"])):
            raise HTTPException(status_code=401, detail="Playback token required")

    # Transfer outcome counters (measured, never modeled)
    served = failed = 0
    bytes_p2p = 0
    for t in data.transfers:
        if t.served:
            served += 1
            bytes_p2p += t.bytes
        else:
            failed += 1
    if served or failed or bytes_p2p:
        await bump_counters(
            data.channel_slug,
            segments_served=served,
            segments_failed=failed,
            bytes_p2p=bytes_p2p,
        )

    failed_seqs = [t.sequence_number for t in data.transfers if not t.served]
    from ..live_state import mark_peer_dead, peer_report
    for seq in failed_seqs:
        # The peer failed to deliver — it loses that segment's availability so
        # other viewers stop trying it (fallback to another peer / origin).
        await mark_peer_dead(data.channel_slug, f"{data.peer_id}:{seq}")
    await peer_report(data.channel_slug, data.peer_id, data.session_id, data.haves)
    return {"ok": True}
