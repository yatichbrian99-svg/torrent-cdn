import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from uuid import UUID

from ..config import get_settings
from ..database import get_db_pool, get_redis
from ..auth import get_current_user_id
from ..models import AnalyticsEventCreate
from ..security import get_client_ip

VALID_EVENTS = {"peer_joined", "peer_left", "chunk_downloaded"}

settings = get_settings()
router = APIRouter(prefix="/analytics", tags=["analytics"])

# In-memory fallback for public analytics rate limiting (used when Redis is down)
_analytics_get_requests: dict[str, list[float]] = {}
_ANALYTICS_GET_WINDOW = 60
# In-memory fallback for the public POST /analytics/event limiter (used when
# Redis is down). This is a WRITE endpoint with no auth, so it MUST be rate
# limited even without Redis — otherwise an attacker can flood the analytics
# table with unbounded inserts while Redis is degraded. Bounded via pruning.
_analytics_event_requests: dict[str, list[float]] = {}
_ANALYTICS_EVENT_WINDOW = 60


def _prune_analytics_get_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _analytics_get_requests.items() if not v or now - v[-1] > _ANALYTICS_GET_WINDOW]
    for k in expired:
        del _analytics_get_requests[k]


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
    if active_peers < 0:
        active_peers = 0

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

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        file_exists = await conn.fetchval(
            "SELECT id FROM uploads WHERE id = $1", event.file_id
        )
        if not file_exists:
            raise HTTPException(status_code=404, detail="File not found")

        await conn.execute(
            """INSERT INTO analytics (file_id, event_type, bytes_p2p, bytes_origin)
               VALUES ($1, $2, $3, $4)""",
            event.file_id,
            event.event_type,
            event.bytes_p2p,
            event.bytes_origin,
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
    today = datetime.utcnow().date()
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
    # Rate limit: 30 requests per minute per IP (Redis primary, in-memory fallback)
    ip = get_client_ip(request)
    redis = await get_redis()
    if redis is not None:
        rate_key = f"analytics_get_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= 30:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        await redis.incr(rate_key)
        await redis.expire(rate_key, 60)
    else:
        now = time.time()
        _prune_analytics_get_requests(now)
        recent = [t for t in _analytics_get_requests.get(ip, []) if now - t < _ANALYTICS_GET_WINDOW]
        if len(recent) >= 30:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        recent.append(now)
        _analytics_get_requests[ip] = recent

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        file_exists = await conn.fetchrow(
            "SELECT id FROM uploads WHERE id = $1", file_id,
        )
        if not file_exists:
            raise HTTPException(status_code=404, detail="File not found")

        return await _aggregate_file_analytics(conn, file_id)


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
    # Rate limit: 30 requests per minute per IP (Redis primary, in-memory fallback)
    ip = get_client_ip(request)
    redis = await get_redis()
    if redis is not None:
        rate_key = f"analytics_get_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= 30:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        await redis.incr(rate_key)
        await redis.expire(rate_key, 60)
    else:
        now = time.time()
        _prune_analytics_get_requests(now)
        recent = [t for t in _analytics_get_requests.get(ip, []) if now - t < _ANALYTICS_GET_WINDOW]
        if len(recent) >= 30:
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
