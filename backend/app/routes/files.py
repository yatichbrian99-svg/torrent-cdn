import asyncio
import hashlib
import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..auth import get_current_user_id, hash_password, verify_password
from ..config import get_settings
from ..database import get_db_pool, get_redis
from ..models import (
    MAX_FILENAME_LEN,
    MAX_FOLDER_PATH_LEN,
    FileDetail,
    FileRenameRequest,
    ShareAccessRequest,
    ShareLinkCreate,
    ShareLinkCreated,
    ShareLinkInfo,
)
from ..security import contains_control_chars, enforce_max_size, get_client_ip

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
            """SELECT id, filename, minio_url, torrent_magnet, file_size_mb, thumbnail_url, folder_path, created_at
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
                folder_path=row.get("folder_path"),
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
            folder_path=row.get("folder_path"),
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


# ── DFS M1: folders / rename ──────────────────────────────────────────────

def _validate_folder_path(path: str | None) -> str | None:
    """Validate a logical folder path BEFORE it reaches the DB.

    Returns the normalized value (or None for root). A folder path is purely
    label data — it names a virtual location, never a physical one — so this
    validation keeps the label well-formed (no separators, no traversal-like
    segments, no control chars, no padded whitespace) rather than guarding a
    filesystem. Longest-legitimate-shape check first so messages stay useful.
    """
    if path is None:
        return None
    path = path.strip()
    if path == "":
        return None
    if len(path) > MAX_FOLDER_PATH_LEN:
        raise HTTPException(status_code=400, detail="Folder path too long (max 512 characters)")
    if "/" in path or "\\" in path:
        raise HTTPException(
            status_code=400, detail="Invalid folder path: use a single folder name (nesting comes later)"
        )
    if path in (".", "..") or path.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid folder path")
    if contains_control_chars(path):
        raise HTTPException(status_code=400, detail="Invalid folder path: control characters are not allowed")
    return path


@router.patch("/{file_id}")
async def rename_file(
    file_id: UUID,
    data: FileRenameRequest,
    user_id: UUID = Depends(get_current_user_id),
):
    """Rename and/or move a file (DFS M1).

    Ownership is enforced by `AND user_id = $...` in every statement — a
    cross-user id resolves to no row and 404s without leaking existence.
    Target-conflict (same owner + folder + filename) is a 409, mirroring the
    convention a filesystem UI expects. A no-op (moving to the current
    location) is idempotent 200.
    """
    # Validate first — cheap rejection before touching the pool.
    new_folder = _validate_folder_path(data.folder_path)
    new_name = data.filename
    if new_name is not None:
        new_name = new_name.strip()
        if not new_name or len(new_name) > MAX_FILENAME_LEN:
            raise HTTPException(status_code=400, detail="Invalid filename")
        # Same rule as a folder path below: a control character (NUL above all)
        # is unstorable in Postgres — the bind below would 500 instead of 400.
        if contains_control_chars(new_name):
            raise HTTPException(
                status_code=400, detail="Invalid filename: control characters are not allowed"
            )

    pool = await get_db_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT filename, folder_path FROM uploads WHERE id = $1 AND user_id = $2 FOR UPDATE",
            file_id, user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="File not found")

        final_name = new_name if new_name is not None else row["filename"]
        final_folder = new_folder if data.folder_path is not None else row["folder_path"]

        # Conflict check against DB state under the row lock.
        if final_folder != row["folder_path"] or final_name != row["filename"]:
            clash = await conn.fetchval(
                """SELECT COUNT(*) FROM uploads
                   WHERE user_id = $1 AND folder_path IS NOT DISTINCT FROM $2
                     AND filename = $3 AND id <> $4""",
                user_id, final_folder, final_name, file_id,
            )
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail="A file with this name already exists in that folder",
                )

        await conn.execute(
            "UPDATE uploads SET filename = $1, folder_path = $2 WHERE id = $3 AND user_id = $4",
            final_name, final_folder, file_id, user_id,
        )

    return FileDetail(
        id=file_id,
        filename=final_name,
        minio_url="",  # not re-fetched: rename never touches storage bytes
        torrent_magnet="",
        file_size_mb=0,
        created_at=datetime.now(UTC),
        folder_path=final_folder,
    )


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


# ── DFS M1: share links ──────────────────────────────────────────────────
# Capability URLs for a file: random token in the path (never the file UUID),
# optional expiry / password / download cap, revocable by the owner.
#
# Revocation semantics (documented, per spec §7): revoking a link prevents ALL
# FUTURE access through it — immediately, including links not yet redeemed.
# It cannot recall bytes a recipient already downloaded. Links are
# private-by-default: nothing is shared until the owner creates one.

# Per-IP budget for share-link RESOLUTION (the public endpoint). Distinct from
# PUBLIC_FILE_LIMIT (embed metadata); guessing tokens is bounded by 256 bits of
# entropy, so this limit targets brute-icing refresh loops, not enumeration.
SHARE_ACCESS_LIMIT = 60  # per minute
MAX_LINKS_PER_FILE = 50

_share_access_requests: dict[str, list[float]] = {}


def _prune_share_access(now: float) -> None:
    expired = [k for k, v in _share_access_requests.items() if not v or now - v[-1] > 60]
    for k in expired:
        del _share_access_requests[k]
    enforce_max_size(_share_access_requests)


def _share_url(token: str, request: Request | None = None) -> str:
    """Build the share URL for a link token.

    Primary source: APP_URL (the canonical public frontend). Defensive
    fallback: the caller's Origin/Referer scheme+host — a browser always sends
    one on same-origin API calls, so a misconfigured/empty APP_URL can never
    silently produce a wrong-host link (this is the same family of bug as an
    email-verification link pointing at localhost).
    """
    base = (settings.app_url or "").rstrip("/")
    if base.startswith(("http://", "https://")):
        parsed = urlparse(base)
        if parsed.hostname:  # has a usable host beyond just a scheme
            return f"{base}/share/{token}"
    # Fallback: derive from the browser's own headers (same-origin API call).
    if request is not None:
        origin = request.headers.get("origin") or ""
        if origin.startswith(("http://", "https://")):
            return f"{origin.rstrip('/')}/share/{token}"
        referer = request.headers.get("referer") or ""
        if referer.startswith(("http://", "https://")):
            try:
                parsed = urlparse(referer)
                if parsed.scheme and parsed.hostname:
                    return f"{parsed.scheme}://{parsed.netloc}/share/{token}"
            except Exception:
                pass
    # Last resort: opaque id-looking URL (still functional as a token — the
    # share page reads the token from anywhere the owner pastes it) but log
    # loudly: this should never happen because APP_URL is validated in prod.
    logger.error(
        "APP_URL unusable (%r) and no Origin/Referer header on share-link "
        "creation — returning token-only share reference",
        settings.app_url,
    )
    return f"/share/{token}"


def _hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/{file_id}/links", response_model=ShareLinkCreated, status_code=201)
async def create_share_link(
    request: Request,
    file_id: UUID,
    data: ShareLinkCreate,
    user_id: UUID = Depends(get_current_user_id),
):
    """Create a share link (owner-only). Plaintext token returned exactly once."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM uploads WHERE id = $1 AND user_id = $2", file_id, user_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="File not found")

        link_count = await conn.fetchval(
            "SELECT COUNT(*) FROM share_links WHERE file_id = $1", file_id
        )
        if link_count >= MAX_LINKS_PER_FILE:
            raise HTTPException(status_code=429, detail="Too many links for this file — revoke some first")

        token = secrets.token_urlsafe(24)  # 192-bit entropy, URL-safe
        expires_at = datetime.now(UTC) + timedelta(hours=data.expires_in_hours) if data.expires_in_hours else None
        password_hash = hash_password(data.password) if data.password else None

        link_row = await conn.fetchrow(
            """INSERT INTO share_links (file_id, owner_id, token_hash, password_hash, max_downloads, expires_at)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, expires_at, max_downloads, download_count, revoked_at, created_at""",
            file_id, user_id, _hash_share_token(token), password_hash, data.max_downloads, expires_at,
        )

    # best-effort cache warm of nothing; link responses are not cached.
    return ShareLinkCreated(
        url=_share_url(token, request),
        token=token,
        expires_at=link_row["expires_at"],
        max_downloads=link_row["max_downloads"],
    )


@router.get("/{file_id}/links", response_model=list[ShareLinkInfo])
async def list_share_links(file_id: UUID, user_id: UUID = Depends(get_current_user_id)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, expires_at, max_downloads, download_count, revoked_at, created_at
               FROM share_links WHERE file_id = $1 AND owner_id = $2 ORDER BY created_at DESC""",
            file_id, user_id,
        )
    base = (settings.app_url or "").rstrip("/")
    return [
        ShareLinkInfo(
            id=r["id"],
            # The list endpoint can NEVER reconstruct the plaintext token (only
            # its hash is stored) — the URL keeps the file id + link id so the
            # owner can still identify the link, and the share page itself asks
            # for the full token when it is needed.
            url=f"{base}/share/{r['id']}",
            expires_at=r["expires_at"],
            max_downloads=r["max_downloads"],
            download_count=r["download_count"],
            revoked=r["revoked_at"] is not None,
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.delete("/links/{link_id}")
async def revoke_share_link(link_id: UUID, user_id: UUID = Depends(get_current_user_id)):
    """Revoke a link: blocks ALL future access through it (immediate).

    Cannot recall bytes a recipient already downloaded — that is documented in
    the share UI too, per spec §7's required honesty about revocation.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE share_links SET revoked_at = NOW() WHERE id = $1 AND owner_id = $2 AND revoked_at IS NULL",
            link_id, user_id,
        )
        if result == "UPDATE 0":
            # Not found OR already revoked OR not owned — one 404 keeps it simple.
            raise HTTPException(status_code=404, detail="Link not found")
    return {"detail": "Link revoked"}


@router.post("/share/{token}")
async def access_share_link(token: str, request: Request):
    """Public resolve of a share link.

    Returns the same public metadata shape the embed player uses
    (filename / minio_url / magnet / size) so a logged-out browser can
    download/stream without an account.
    """
    # Token format gate BEFORE any DB/redis touch: a plaintext token is
    # token_urlsafe(24) → 32 chars of [A-Za-z0-9_-]. Reject everything else.
    if not token or len(token) != 32 or not all(c.isalnum() or c in "-_" for c in token):
        raise HTTPException(status_code=404, detail="Link not found")

    # Rate limit per IP (in-memory; Redis optional).
    ip = get_client_ip(request)
    now = time.time()
    _prune_share_access(now)
    recent = [t for t in _share_access_requests.get(ip, []) if now - t < 60]
    if len(recent) >= SHARE_ACCESS_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
    recent.append(now)
    _share_access_requests[ip] = recent

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT s.id, s.password_hash, s.max_downloads, s.download_count,
                      s.expires_at, s.revoked_at, s.owner_id,
                      u.id AS file_id, u.filename, u.minio_url, u.torrent_magnet, u.file_size_mb
               FROM share_links s JOIN uploads u ON u.id = s.file_id
               WHERE s.token_hash = $1""",
            _hash_share_token(token),
        )

        if not row:
            raise HTTPException(status_code=404, detail="Link not found")
        if row["revoked_at"] is not None:
            raise HTTPException(status_code=410, detail="This link has been revoked")
        if row["expires_at"] is not None and datetime.now(UTC) > row["expires_at"]:
            raise HTTPException(status_code=410, detail="This link has expired")
        if row["max_downloads"] is not None and row["download_count"] >= row["max_downloads"]:
            raise HTTPException(status_code=410, detail="This link has reached its download limit")

        if row["password_hash"]:
            # Password-protected link: the browser POSTs the password in the
            # body (ShareAccessRequest); a GET or a missing/wrong password
            # gets 401 without revealing WHY the link exists.
            provided: str | None = None
            try:
                body = await request.json()
                if isinstance(body, dict):
                    provided = ShareAccessRequest(**body).password
            except Exception:
                provided = None
            if not provided or not verify_password(provided, row["password_hash"]):
                raise HTTPException(status_code=401, detail="Password required")

        # Count the download (best-effort: a transient failure here must not
        # block access — an undercount is safer than a failed grant).
        try:
            await conn.execute(
                "UPDATE share_links SET download_count = download_count + 1 WHERE id = $1",
                row["id"],
            )
        except Exception as e:
            logger.warning("Share link download count update failed: %s", e)

    # NOTE: response intentionally mirrors the /files/public/{id} payload so the
    # frontend share page reuses the same player/downloader component. The
    # minio_url is a presigned-capable storage URL the recipient fetches
    # directly from object storage — the API still does not proxy bytes.
    return {
        "id": str(row["file_id"]),
        "filename": row["filename"],
        "minio_url": row["minio_url"],
        "magnet_link": row["torrent_magnet"],
        "file_size_mb": row["file_size_mb"],
    }
