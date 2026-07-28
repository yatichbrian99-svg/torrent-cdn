from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from ..database import get_db_pool
from ..models import AnalyticsEventCreate

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.post("/event")
async def track_event(event: AnalyticsEventCreate):
    """Track an analytics event. Public endpoint — no auth required."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Verify file exists
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
async def get_analytics(file_id: UUID):
    """Get analytics for a file. Public endpoint."""
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

        # Count active peers (joined - left)
        active_peers = (result["total_views"] or 0) - (result["total_leaves"] or 0)
        if active_peers < 0:
            active_peers = 0

        return {
            "total_views": result["total_views"] or 0,
            "p2p_offload_percent": round(p2p_offload_percent, 1),
            "bandwidth_saved_gb": round(bandwidth_saved_gb, 3),
            "active_peers": active_peers,
        }
