import time
from fastapi import APIRouter, Depends, HTTPException, Request
from uuid import UUID

from ..config import get_settings
from ..database import get_db_pool, get_redis
from ..models import AnalyticsEventCreate

VALID_EVENTS = {"peer_joined", "peer_left", "chunk_downloaded"}

settings = get_settings()
router = APIRouter(prefix="/analytics", tags=["analytics"])

# In-memory fallback for public analytics rate limiting (used when Redis is down)
_analytics_get_requests: dict[str, list[float]] = {}
_ANALYTICS_GET_WINDOW = 60


def _prune_analytics_get_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _analytics_get_requests.items() if not v or now - v[-1] > _ANALYTICS_GET_WINDOW]
    for k in expired:
        del _analytics_get_requests[k]


@router.post("/event")
async def track_event(
    event: AnalyticsEventCreate,
    request: Request,
):
    """Track an analytics event. Public endpoint — no auth required."""
    # Validate event type
    if event.event_type not in VALID_EVENTS:
        raise HTTPException(status_code=400, detail="Invalid event type")

    # IP-based rate limiting (skip if Redis unavailable)
    redis = await get_redis()
    if redis is not None:
        ip = request.client.host if request.client else "unknown"
        rate_key = f"analytics_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= settings.rate_limit_analytics:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        await redis.incr(rate_key)
        await redis.expire(rate_key, 60)

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


@router.get("/{file_id}")
async def get_analytics(file_id: UUID, request: Request):
    """Get analytics for a file. Public endpoint."""
    # Rate limit: 30 requests per minute per IP (Redis primary, in-memory fallback)
    ip = request.client.host if request.client else "unknown"
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
        file_exists = await conn.fetchval(
            "SELECT id FROM uploads WHERE id = $1", file_id
        )
        if not file_exists:
            raise HTTPException(status_code=404, detail="File not found")

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
        }
