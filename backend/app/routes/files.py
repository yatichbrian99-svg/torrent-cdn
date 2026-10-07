import asyncio
import json
import logging
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..auth import get_current_user_id
from ..config import get_settings
from ..database import get_db_pool, get_redis
from ..models import FileDetail
from ..security import enforce_max_size, get_client_ip

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/files", tags=["files"])

# In-memory fallback for public file rate limiting (used when Redis is down)
_public_file_requests: dict[str, list[float]] = {}
_PUBLIC_FILE_WINDOW = 60
# Public file-metadata GET budget per IP per minute. 120/min is generous for
# embed players (one metadata fetch per page load, Redis-cached for 60s) while
# still capping scrapers.
PUBLIC_FILE_LIMIT = 120
# M3 FIX: Per-IP 404 counter to prevent file-ID enumeration. An attacker
# probing random UUIDs would hit 404s; after this many unique-file 404s per
# minute the IP is throttled. Legitimate embeds rarely see 404s (files exist).
_PUBLIC_FILE_404_LIMIT = 10
_public_file_404s: dict[str, list[float]] = {}

# Public metadata cache TTL. The payload (filename/url/magnet/size) is immutable
# after upload, so every embed for a popular file can be answered from Redis
# instead of a DB round-trip per page load. A deleted file resolves stale for at
# most this long — playback still fails at the storage URL (403/404), so the
# staleness window is harmless; delete_file invalidates the key immediately.
PUBLIC_FILE_CACHE_TTL = 60

# Single-flight lock: when a hot file's cache entry EXPIRES, every concurrent
# viewer miss would otherwise hit the DB at once (a thundering herd — exactly
# at the start of a traffic spike, the worst moment). The first miss re-populates
# the cache while the rest wait on this lock and then read the fresh entry.
# Redis SET NX: works across all gunicorn workers.
_public_file_lock_key = "public_file_lock:{file_id}"

def _cache_control_public(ttl: int) -> str:
    """Shared-cache policy for the public viewer hot path.

    max-age lets the Vercel edge / any CDN serve repeated metadata GETs for the
    same file without touching the origin at all (at 200k viewers the embed
    page-load burst is the largest request class); stale-while-revalidate keeps
    serving the previous payload while one request refreshes. The payload is
    PUBLIC and immutable per file id (delete invalidates server-side), so a
    shared cache holding it briefly leaks nothing.
    """
    return f"public, max-age={ttl}, stale-while-revalidate={ttl}"


def _prune_public_file_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _public_file_requests.items() if not v or now - v[-1] > _PUBLIC_FILE_WINDOW]
    for k in expired:
        del _public_file_requests[k]
    enforce_max_size(_public_file_requests)


# Top files to pre-warm after deploy (most-viewed first). This list is small
# enough to batch-fetch from the DB in a single query and store in Redis
# without meaningful overhead.
CACHE_WARM_TOP_N = 50


async def warm_public_file_cache() -> int:
    """Pre-warm Redis cache for the most popular public files.

    Called once at startup (after migrations) so the first viewers after a
    deploy don't all miss the cache simultaneously. The cache TTL is short
    (60s), so warming is only valuable in the narrow post-deploy window —
    after that the natural request pattern re-populates the cache.

    Returns the number of files cached (best-effort, never fatal).
    """
    try:
        redis = await get_redis()
        if redis is None:
            return 0
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT u.id, u.filename, u.minio_url, u.torrent_magnet, u.file_size_mb
                   FROM uploads u
                   LEFT JOIN (
                       SELECT file_id, COUNT(*) AS views
                       FROM analytics
                       WHERE event_type = 'peer_joined'
                         AND created_at > NOW() - INTERVAL '7 days'
                       GROUP BY file_id
                   ) a ON a.file_id = u.id
                   ORDER BY COALESCE(a.views, 0) DESC
                   LIMIT $1""",
                CACHE_WARM_TOP_N,
            )
            if not rows:
                return 0
            pipe = redis.pipeline()
            for row in rows:
                cache_key = f"public_file:{row['id']}"
                payload = json.dumps({
                    "id": str(row["id"]),
                    "filename": row["filename"],
                    "minio_url": row["minio_url"],
                    "magnet_link": row["torrent_magnet"],
                    "file_size_mb": row["file_size_mb"],
                }, default=str)
                pipe.setex(cache_key, PUBLIC_FILE_CACHE_TTL, payload)
            await pipe.execute()
            logger.info("Cache warmed for %d popular files", len(rows))
            return len(rows)
    except Exception as e:
        logger.warning("Cache warming skipped: %s", e)
        return 0


# Pagination caps for GET /files: the response shape stays a bare array (the
# frontend reads it directly), but an unbounded query would let one user with a
# huge library force an arbitrarily large response. Default 100, hard cap 500.
MAX_LIST_LIMIT = 500
DEFAULT_LIST_LIMIT = 100


@router.get("")
async def list_files(
    user_id: UUID = Depends(get_current_user_id),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, filename, minio_url, torrent_magnet, file_size_mb, thumbnail_url, created_at
               FROM uploads WHERE user_id = $1
               ORDER BY created_at DESC
               LIMIT $2 OFFSET $3""",
            user_id, limit, offset,
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
                thumbnail_url=row.get("thumbnail_url"),
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
            thumbnail_url=row.get("thumbnail_url"),
        )


def _with_webseed_read(magnet: str | None, minio_url: str | None) -> str | None:
    """Read-time webseed annotation for magnets stored BEFORE ws= existed.

    New uploads get their webseed baked in at write time (routes/upload.py);
    every magnet already in the DB gets it HERE, at read time, so the whole
    existing catalogue gains the origin fallback with zero migration. The ws=
    param does not change the infohash, so this cannot fork a swarm. Idempotent
    (a magnet already carrying ws= is untouched) and total: on any error the
    stored magnet is returned as-is.
    """
    try:
        if not magnet or not minio_url or "ws=" in magnet:
            return magnet
        from ..torrent_builder import with_webseed
        return with_webseed(magnet, minio_url)
    except Exception:
        return magnet


@router.get("/public/{file_id}")
async def get_public_file(file_id: UUID, request: Request = None):
    """Public endpoint for embed player — no auth required."""
    # Rate limit: 120 requests per minute per IP for public file access
    # (Redis primary, in-memory fallback when Redis is unavailable)
    redis = await get_redis()
    if request:
        ip = get_client_ip(request)
        rate_key = f"public_file_rate:{ip}"
        if redis is not None:
            count = await redis.get(rate_key)
            if count and int(count) >= PUBLIC_FILE_LIMIT:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            await redis.incr(rate_key)
            await redis.expire(rate_key, 60)
        else:
            now = time.time()
            _prune_public_file_requests(now)
            recent = [t for t in _public_file_requests.get(ip, []) if now - t < _PUBLIC_FILE_WINDOW]
            if len(recent) >= PUBLIC_FILE_LIMIT:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            recent.append(now)
            _public_file_requests[ip] = recent

    # Redis cache (global per file — the payload is public and immutable). A
    # Redis error is a miss: fall through to the DB rather than 500.
    cache_key = f"public_file:{file_id}"
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                payload = json.loads(cached)
                # Read-time webseed annotation for legacy magnets (see
                # _with_webseed_read) — only when the cached entry predates it.
                payload["magnet_link"] = _with_webseed_read(
                    payload.get("magnet_link"), payload.get("minio_url")
                )
                return JSONResponse(payload, headers={"Cache-Control": _cache_control_public(PUBLIC_FILE_CACHE_TTL)})
        except HTTPException:
            raise
        except Exception:
            pass

    # Single-flight: one worker re-populates the expired cache entry while the
    # rest block briefly on the lock (then read the fresh entry). Without this
    # a hot file's TTL expiry turns every concurrent embed load into a
    # simultaneous DB hit — a thundering herd exactly when traffic spikes.
    lock_key = _public_file_lock_key.format(file_id=file_id)
    lock_acquired = False
    if redis is not None:
        try:
            lock_acquired = bool(await redis.set(lock_key, "1", nx=True, ex=5))
        except Exception:
            lock_acquired = False
        if not lock_acquired:
            # Someone else is loading it — wait for the fresh entry instead of
            # also hitting the DB. Bounded: 5 x 100ms, then fall through (a
            # stuck worker holds the lock only 5s, and the DB path is correct).
            for _ in range(5):
                await asyncio.sleep(0.1)
                try:
                    cached = await redis.get(cache_key)
                    if cached:
                        payload = json.loads(cached)
                        payload["magnet_link"] = _with_webseed_read(
                            payload.get("magnet_link"), payload.get("minio_url")
                        )
                        return JSONResponse(payload, headers={"Cache-Control": _cache_control_public(PUBLIC_FILE_CACHE_TTL)})
                except Exception:
                    break

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, filename, minio_url, torrent_magnet, file_size_mb
               FROM uploads WHERE id = $1""",
            file_id,
        )
        if not row:
            # M3 FIX: Track 404s per IP to prevent file-ID enumeration.
            # An attacker probing random UUIDs would otherwise learn which
            # UUIDs are valid vs invalid at 120/min without limit.
            if request:
                _ip = get_client_ip(request)
                if redis is not None:
                    _404_key = f"public_file_404:{_ip}"
                    _404_count = await redis.incr(_404_key)
                    if _404_count == 1:
                        await redis.expire(_404_key, 60)
                    if _404_count > _PUBLIC_FILE_404_LIMIT:
                        raise HTTPException(status_code=429, detail="Rate limit exceeded")
                else:
                    now404 = time.time()
                    recent404 = [t for t in _public_file_404s.get(_ip, []) if now404 - t < 60]
                    recent404.append(now404)
                    _public_file_404s[_ip] = recent404
                    if len(recent404) > _PUBLIC_FILE_404_LIMIT:
                        raise HTTPException(status_code=429, detail="Rate limit exceeded")
            raise HTTPException(status_code=404, detail="File not found")

        # NOTE: minio_url contains the user_id UUID in the path. We cannot
        # strip it because R2 needs the full path to serve the file. The
        # embed player streams directly from this URL. To hide the user_id
        # would require a proxy endpoint (architectural change).
        payload = {
            "id": str(row["id"]),
            "filename": row["filename"],
            "minio_url": row["minio_url"],
            "magnet_link": _with_webseed_read(row["torrent_magnet"], row["minio_url"]),
            "file_size_mb": row["file_size_mb"],
        }

    if redis is not None:
        try:
            await redis.setex(cache_key, PUBLIC_FILE_CACHE_TTL, json.dumps(payload, default=str))
        except Exception:
            pass  # cache write is best-effort — the DB read already succeeded

    return JSONResponse(payload, headers={"Cache-Control": _cache_control_public(PUBLIC_FILE_CACHE_TTL)})


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

        minio_url = row["minio_url"]
        # Delete the DB row FIRST so the refcount check below excludes THIS row
        # (content-dedup means other uploads may share the same stored object).
        await conn.execute("DELETE FROM uploads WHERE id = $1 AND user_id = $2", file_id, user_id)

        # Delete from MinIO only when no OTHER upload references the object.
        # The key is the last 3 URL segments ({user_id}/{file_id}/{filename}) —
        # same extraction the account-deletion path uses, robust to the bucket
        # name appearing in different URL shapes (MinIO path-style vs Supabase
        # object/public/<bucket>). A storage failure is logged (the production
        # orphan sweep eventually reclaims the object — its row is gone now),
        # never fatal.
        try:
            from ..routes.upload import get_minio_client
            s3 = get_minio_client()
            key = "/".join(minio_url.split("/")[-3:])
            refs = await conn.fetchval(
                "SELECT COUNT(*) FROM uploads WHERE minio_url = $1", minio_url
            )
            if refs == 0:
                s3.delete_object(Bucket=settings.minio_bucket, Key=key)
        except Exception as e:
            logger.warning("Failed to delete storage object for file %s: %s", file_id, e)

    # Drop the public-metadata cache entry so an embed resolves 404 immediately
    # instead of serving stale metadata for up to PUBLIC_FILE_CACHE_TTL.
    try:
        redis = await get_redis()
        if redis is not None:
            await redis.delete(f"public_file:{file_id}")
    except Exception:
        pass

    return {"detail": "File deleted successfully"}
