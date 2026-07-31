import time
from fastapi import APIRouter, Depends, HTTPException, Request
from uuid import UUID

from ..database import get_db_pool, get_redis
from ..auth import get_current_user_id
from ..models import FileDetail

router = APIRouter(prefix="/files", tags=["files"])

# In-memory fallback for public file rate limiting (used when Redis is down)
_public_file_requests: dict[str, list[float]] = {}
_PUBLIC_FILE_WINDOW = 60


def _prune_public_file_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _public_file_requests.items() if not v or now - v[-1] > _PUBLIC_FILE_WINDOW]
    for k in expired:
        del _public_file_requests[k]


@router.get("")
async def list_files(user_id: UUID = Depends(get_current_user_id)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, filename, minio_url, torrent_magnet, file_size_mb, created_at
               FROM uploads WHERE user_id = $1
               ORDER BY created_at DESC""",
            user_id,
        )

        file_ids = [row["id"] for row in rows]

        # Single batch query for all analytics
        analytics_by_file: dict[UUID, dict] = {}
        if file_ids:
            analytics_rows = await conn.fetch(
                """SELECT file_id,
                           COUNT(*) FILTER (WHERE event_type = 'peer_joined') as views,
                           COALESCE(SUM(bytes_p2p), 0) as total_p2p,
                           COALESCE(SUM(bytes_origin), 0) as total_origin
                      FROM analytics
                     WHERE file_id = ANY($1::uuid[])
                     GROUP BY file_id""",
                file_ids,
            )
            for a in analytics_rows:
                analytics_by_file[a["file_id"]] = {
                    "views": a["views"],
                    "total_p2p": a["total_p2p"],
                    "total_origin": a["total_origin"],
                }

        results = []
        for row in rows:
            a = analytics_by_file.get(row["id"], {"views": 0, "total_p2p": 0, "total_origin": 0})
            total_bytes = (a["total_p2p"] or 0) + (a["total_origin"] or 0)
            p2p_offload = 0
            bandwidth_saved = 0
            if total_bytes > 0:
                p2p_offload = (a["total_p2p"] / total_bytes) * 100
                bandwidth_saved = a["total_p2p"] / (1024 * 1024 * 1024)

            results.append(FileDetail(
                id=row["id"],
                filename=row["filename"],
                minio_url=row["minio_url"],
                torrent_magnet=row["torrent_magnet"],
                file_size_mb=row["file_size_mb"],
                created_at=row["created_at"],
                views=a["views"] or 0,
                p2p_offload_percent=round(p2p_offload, 1),
                bandwidth_saved_gb=round(bandwidth_saved, 3),
                active_peers=0,
            ))

        return results


@router.get("/{file_id}", response_model=FileDetail)
async def get_file(file_id: UUID, user_id: UUID = Depends(get_current_user_id)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM uploads WHERE id = $1 AND user_id = $2""",
            file_id, user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="File not found")

        analytics = await conn.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE event_type = 'peer_joined') as views,
                   COALESCE(SUM(bytes_p2p), 0) as total_p2p,
                   COALESCE(SUM(bytes_origin), 0) as total_origin
               FROM analytics WHERE file_id = $1""",
            file_id,
        )

        total_bytes = (analytics["total_p2p"] or 0) + (analytics["total_origin"] or 0)
        p2p_offload = 0
        bandwidth_saved = 0
        if total_bytes > 0:
            p2p_offload = (analytics["total_p2p"] / total_bytes) * 100
            bandwidth_saved = analytics["total_p2p"] / (1024 * 1024 * 1024)

        return FileDetail(
            id=row["id"],
            filename=row["filename"],
            minio_url=row["minio_url"],
            torrent_magnet=row["torrent_magnet"],
            file_size_mb=row["file_size_mb"],
            created_at=row["created_at"],
            views=analytics["views"] or 0,
            p2p_offload_percent=round(p2p_offload, 1),
            bandwidth_saved_gb=round(bandwidth_saved, 3),
            active_peers=0,
        )


@router.get("/public/{file_id}")
async def get_public_file(file_id: UUID, request: Request = None):
    """Public endpoint for embed player — no auth required."""
    # Rate limit: 30 requests per minute per IP for public file access
    # (Redis primary, in-memory fallback when Redis is unavailable)
    redis = await get_redis()
    if request:
        ip = request.client.host if request.client else "unknown"
        rate_key = f"public_file_rate:{ip}"
        if redis is not None:
            count = await redis.get(rate_key)
            if count and int(count) >= 30:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            await redis.incr(rate_key)
            await redis.expire(rate_key, 60)
        else:
            now = time.time()
            _prune_public_file_requests(now)
            recent = [t for t in _public_file_requests.get(ip, []) if now - t < _PUBLIC_FILE_WINDOW]
            if len(recent) >= 30:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            recent.append(now)
            _public_file_requests[ip] = recent

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, filename, minio_url, torrent_magnet, file_size_mb
               FROM uploads WHERE id = $1""",
            file_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="File not found")

        return {
            "id": str(row["id"]),
            "filename": row["filename"],
            "minio_url": row["minio_url"],
            "magnet_link": row["torrent_magnet"],
            "file_size_mb": row["file_size_mb"],
        }


@router.delete("/{file_id}")
async def delete_file(file_id: UUID, user_id: UUID = Depends(get_current_user_id)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT minio_url FROM uploads WHERE id = $1 AND user_id = $2",
            file_id, user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="File not found")

        # Delete from MinIO
        try:
            from ..routes.upload import get_minio_client
            s3 = get_minio_client()
            # Extract key from URL
            minio_url = row["minio_url"]
            key = minio_url.split(f"{settings.minio_bucket}/")[-1]
            s3.delete_object(Bucket=settings.minio_bucket, Key=key)
        except Exception:
            pass  # Non-critical failure

        # Delete from DB
        await conn.execute("DELETE FROM uploads WHERE id = $1 AND user_id = $2", file_id, user_id)

    return {"detail": "File deleted successfully"}
