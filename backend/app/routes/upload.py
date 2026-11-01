import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse
from uuid import UUID

import boto3
import httpx
from asyncpg.exceptions import UniqueViolationError
from botocore.config import Config as BotoConfig
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from ..auth import get_current_user_id
from ..config import get_settings
from ..database import get_db_pool, get_redis
from ..malware_scan import scan_file
from ..models import (
    ChunkUploadResponse,
    UploadImportRequest,
    UploadPresignRequest,
    UploadPresignResponse,
    UploadResponse,
    UploadSessionRequest,
    UploadSessionResponse,
)

# The pinning transport (httpcore's network-backend hook, which is what lets
# the import fetch dial the validated address and nothing else) is shared with
# the AI engine's origin fetch — one implementation, so the two cannot drift.
# Imported under the historical private aliases because this module's call
# sites and tests use them.
from ..pinned_http import (
    PinnedHTTPTransport as _PinnedHTTPTransport,
)
from ..security import (
    EXTENSION_MIME_MAP,
    MAX_COMPRESSION_RATIO,
    enforce_max_size,
    estimate_compression_ratio,
    get_client_ip,
    validate_content_type,
    validate_file_magic,
)
from ..torrent_builder import build_magnet, hash_pieces_from_stream, with_webseed

# Per-user caps on concurrent in-flight upload sessions (chunked + presign).
# An authenticated attacker who can't be rate-limited per-IP (XFF spoofing,
# or simply a distributed botnet) could otherwise mint unbounded sessions:
# chunked sessions hold a growing temp file on disk and presign sessions
# accumulate in Redis/in-memory. Capping active sessions per user bounds both
# memory and temp-disk exhaustion.
#
# NOTE on counting: the cap counts only sessions that can STILL GROW (fewer
# chunks received than declared). A fully-received session can no longer write
# to its temp file (offsets are bounded by total_chunks and the write-size
# check below enforces the declared file_size), so excluding it from the count
# prevents a false 429 on a legitimate power user doing many chunked-fallback
# uploads back-to-back (chunk sessions are intentionally kept 30 min past
# finalize for lost-response finalize retries, so counting them all would lock
# out real users after ~20 uploads). Temp-disk is still bounded: a completed
# session's file can never exceed the declared (≤1GB) size, and the TTL prune
# reclaims it.
MAX_ACTIVE_CHUNK_SESSIONS_PER_USER = 50
MAX_ACTIVE_PRESIGN_SESSIONS_PER_USER = 50

CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB per multipart chunk
# Client-side chunked-upload fallback: the frontend slices the file into chunks
# of EXACTLY this size and ships them through the same-origin Vercel proxy, so
# each chunk must stay under the proxy's ~4.5MB request-body cap (verified
# empirically: 4MB passes, 5MB is rejected). MUST match the frontend constant.
MAX_CLIENT_CHUNK_SIZE = 4 * 1024 * 1024
CHUNK_SESSION_TTL = 30 * 60  # chunked-upload sessions expire after 30 min

# Presigned direct-to-Supabase uploads (the PRIMARY upload path): the backend
# issues a short-lived PUT URL and the browser ships the file bytes STRAIGHT to
# storage — never through Render or the Vercel proxy. Keep the expiry short so
# an intercepted URL can't be abused later; the client must start the PUT
# promptly after presigning.
PRESIGN_URL_TTL = 900  # 15 min — signed PUT window
PRESIGN_SESSION_TTL = 30 * 60  # presign sessions expire after 30 min

# Orphaned-object sweep: an object that was PUT but never finalized (presign
# session expired, or the DB insert crashed after storage write) is garbage in
# the bucket. A production-only background task lists the bucket and deletes
# objects that have NO uploads row AND NO pending session AND are older than the
# grace window (so an in-flight direct/chunked upload can never be raced).
ORPHAN_GRACE_SECONDS = 60 * 60  # never touch objects younger than 1h
# Sweep cadence (default 15 min) — overridable via ORPHAN_SWEEP_INTERVAL_SECONDS;
# assigned from settings after `settings = get_settings()` below.
ORPHAN_SWEEP_MAX_DELETIONS = 200  # per run — a runaway bucket can't stall one sweep

logger = logging.getLogger(__name__)

settings = get_settings()
ORPHAN_SWEEP_INTERVAL_SECONDS = settings.orphan_sweep_interval_seconds
router = APIRouter(prefix="/upload", tags=["upload"])

ALLOWED_EXTS = {
    ".mp4",
    ".webm",
    ".ogv",
    ".mov",
    ".mp3",
    ".ogg",
    ".wav",
    ".m4a",
    ".avi",
    ".mkv",
}

VIDEO_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".avi", ".mkv"}

# Usage-alert thresholds: (fraction of plan limit, notification type, message)
USAGE_THRESHOLDS = [
    (
        0.50,
        "usage_50",
        "You've used 50% of your monthly bandwidth. Upgrade to avoid interruption.",
    ),
    (
        0.80,
        "usage_80",
        "You've used 80% of your monthly bandwidth. Upgrade now to keep delivering.",
    ),
    (
        0.95,
        "usage_95",
        "You're almost out of bandwidth. Upgrade immediately to avoid service interruption.",
    ),
]

# In-memory fallback for the per-IP upload rate limit (used when Redis is down)
_upload_requests: dict[str, list[float]] = {}

# Chunked-upload sessions (client-side chunking fallback). Redis-backed with an
# in-memory fallback, mirroring the presign sessions: the session dict (metadata,
# received-chunk set, assembly temp-file path) is written to Redis with TTL so
# MULTIPLE gunicorn workers / an instance recycle never split a session (created
# on worker A, chunked on worker B). The in-memory dict is the Redis-down
# fallback (single worker then, same as before). Sessions expire via
# _prune_chunk_sessions (Redis TTL is the backstop; the prune deletes the
# assembly temp file); finalize never deletes the session so a lost-response
# retry of finalize can re-run safely (idempotency dedup returns the stored
# upload instead of duplicating it).
_chunk_sessions: dict[str, dict] = {}


def _chunk_session_key(upload_id: str) -> str:
    return f"chunk_session:{upload_id}"


# Presigned-upload sessions (primary direct-to-storage path). Holds the object
# key + declared size so finalize can verify the browser's PUT without trusting
# client-supplied metadata. Expired via _prune_presign_sessions (no temp files
# on disk, so pruning is just dict cleanup).
_presign_sessions: dict[str, dict] = {}


def _prune_presign_sessions(now: float | None = None) -> None:
    """Drop expired in-memory presign session copies (Redis copies self-expire
    via TTL). DELIBERATELY does NOT delete the session's object from storage:
    a previous version did, and because finalize never removed the session, ~30
    min after a SUCCESSFUL upload the next presign/finalize call deleted a
    FINALIZED user file. Object GC is the orphan sweep's job (sweep_orphan_objects),
    which checks the uploads table first so a finalized object is never deleted."""
    now = now or time.time()
    expired = [
        k
        for k, s in _presign_sessions.items()
        if now - s["created_at"] > PRESIGN_SESSION_TTL
    ]
    for k in expired:
        _presign_sessions.pop(k, None)


def _presign_session_key(upload_id: str) -> str:
    return f"presign_session:{upload_id}"


def _presign_session_idx_key(user_id, idempotency_key: str) -> str:
    return f"presign_idx:{user_id}:{idempotency_key}"


async def _load_presign_session(
    upload_id: str, raise_on_error: bool = False
) -> dict | None:
    """Load a presign session: the in-memory copy first (fast, always current),
    then Redis (survives instance recycling — the JSON round-trip stores
    user_id as str).

    By default a Redis read error is treated as a miss (the caller can fall
    back safely). With raise_on_error=True the error propagates — used by the
    orphan sweep so an unreadable session is NEVER mistaken for "no session"
    (fail-safe: skip the object rather than delete it)."""
    session = _presign_sessions.get(upload_id)
    if session is not None:
        return session
    try:
        redis = await get_redis()
        if redis is not None:
            raw = await redis.get(_presign_session_key(upload_id))
            if raw:
                return json.loads(raw)
    except Exception:
        if raise_on_error:
            raise
        # Redis hiccup — treat as a miss; the memory copy was already checked
    return None


async def _save_presign_session(upload_id: str, session: dict) -> None:
    """Persist a presign session: ALWAYS to the in-memory dict (fast path,
    keeps idempotent-reuse simple), and to Redis with TTL when available so the
    session survives a Render instance recycle between presign and finalize."""
    _presign_sessions[upload_id] = session
    try:
        redis = await get_redis()
        if redis is not None:
            await redis.setex(
                _presign_session_key(upload_id),
                PRESIGN_SESSION_TTL,
                json.dumps(session, default=str),
            )
            if session.get("idempotency_key"):
                await redis.setex(
                    _presign_session_idx_key(
                        session["user_id"], session["idempotency_key"]
                    ),
                    PRESIGN_SESSION_TTL,
                    upload_id,
                )
    except Exception:
        pass  # Redis down — the in-memory copy still holds the session


async def _delete_presign_session(upload_id: str) -> None:
    """Remove a completed/abandoned session from both stores."""
    session = _presign_sessions.pop(upload_id, None)
    try:
        redis = await get_redis()
        if redis is not None:
            await redis.delete(_presign_session_key(upload_id))
            if session and session.get("idempotency_key"):
                await redis.delete(
                    _presign_session_idx_key(
                        session["user_id"], session["idempotency_key"]
                    )
                )
    except Exception:
        pass


async def _find_presign_session_by_key(user_id: UUID, idempotency_key: str):
    """Reuse a PENDING session for the same (user, idempotency_key) — same
    upload_id/object key, fresh URL. Checks the in-memory dict first, then the
    Redis index (covers a fresh instance whose memory is cold)."""
    for uid, s in _presign_sessions.items():
        if (
            str(s.get("user_id")) == str(user_id)
            and s.get("idempotency_key") == idempotency_key
        ):
            return uid, s
    try:
        redis = await get_redis()
        if redis is not None:
            found_id = await redis.get(
                _presign_session_idx_key(user_id, idempotency_key)
            )
            if found_id:
                session = await _load_presign_session(found_id)
                if session:
                    return found_id, session
    except Exception:
        pass
    return None


def _remove_chunk_temp_file(path) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


async def _prune_chunk_sessions(now: float | None = None) -> None:
    """Drop expired chunked-upload sessions and their assembly temp files.

    Prunes the in-memory copies AND Redis sessions (Redis TTL is only a
    backstop — a key that expires via TTL would otherwise leak its assembly
    temp file until the next prune). Redis hiccups are ignored: the TTL
    backstop still expires the keys. Runs lazily on upload activity, matching
    the previous behavior."""
    now = now or time.time()
    expired = [
        k
        for k, s in _chunk_sessions.items()
        if now - s["created_at"] > CHUNK_SESSION_TTL
    ]
    for k in expired:
        s = _chunk_sessions.pop(k)
        _remove_chunk_temp_file(s.get("path"))
    try:
        redis = await get_redis()
        if redis is not None:
            async for key in redis.scan_iter("chunk_session:*", count=100):
                raw = await redis.get(key)
                if not raw:
                    continue
                try:
                    s = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if now - s.get("created_at", 0) > CHUNK_SESSION_TTL:
                    _remove_chunk_temp_file(s.get("path"))
                    await redis.delete(key)
    except Exception:
        pass  # Redis hiccup — the TTL backstop still expires the keys


async def _load_chunk_session(upload_id: str) -> dict | None:
    """Load a chunk session: the Redis copy first (authoritative across
    workers — the session may have been created on another worker), then the
    in-memory fallback. A Redis error is treated as a miss."""
    try:
        redis = await get_redis()
        if redis is not None:
            raw = await redis.get(_chunk_session_key(upload_id))
            if raw:
                session = json.loads(raw)
                # JSON round-trip: received is a list, user_id a string.
                session["received"] = set(session.get("received") or [])
                if isinstance(session.get("user_id"), str):
                    try:
                        session["user_id"] = UUID(session["user_id"])
                    except ValueError:
                        pass
                return session
    except Exception:
        pass
    return _chunk_sessions.get(upload_id)


async def _save_chunk_session(upload_id: str, session: dict) -> None:
    """Persist a chunk session: ALWAYS to the in-memory dict (fast path +
    Redis-down fallback) AND to Redis with TTL when available (so a session
    created on one worker can be continued on another). received is a set in
    memory but a sorted JSON list in Redis."""
    _chunk_sessions[upload_id] = session
    try:
        redis = await get_redis()
        if redis is not None:
            payload = dict(session)
            payload["received"] = sorted(session.get("received") or [])
            await redis.setex(
                _chunk_session_key(upload_id),
                CHUNK_SESSION_TTL,
                json.dumps(payload, default=str),
            )
    except Exception:
        pass  # Redis down — the in-memory copy still holds the session


async def _active_chunk_sessions(user_id) -> list:
    """Growable (received < total_chunks) chunk sessions for the user — Redis
    first (covers sessions started on other workers), in-memory fallback."""
    sessions: list = []
    seen: set = set()
    try:
        redis = await get_redis()
        if redis is not None:
            async for key in redis.scan_iter("chunk_session:*", count=100):
                upload_id = key.split(":", 1)[1] if ":" in key else key
                s = await _load_chunk_session(upload_id)
                if s and str(s.get("user_id")) == str(user_id):
                    sessions.append(s)
                    seen.add(upload_id)
    except Exception:
        pass
    for uid, s in _chunk_sessions.items():
        if uid not in seen and str(s.get("user_id")) == str(user_id):
            sessions.append(s)
    return [
        s
        for s in sessions
        if len(s.get("received") or set()) < s.get("total_chunks", 0)
    ]


def _prune_upload_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [
        k
        for k, v in _upload_requests.items()
        if not v or now - v[-1] > settings.rate_limit_uploads_window
    ]
    for k in expired:
        del _upload_requests[k]
    enforce_max_size(_upload_requests)


def _upload_limit_detail() -> str:
    """Human-readable 429 detail for the per-IP upload limiter."""
    window = settings.rate_limit_uploads_window
    if window >= 3600 and window % 3600 == 0:
        unit = "hour" if window == 3600 else f"{window // 3600} hours"
    else:
        unit = f"{window}s"
    return f"Upload limit reached ({settings.rate_limit_uploads} per {unit} per IP). Please try again later."


def get_minio_client():
    protocol = "https" if settings.minio_secure else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{protocol}://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name=settings.minio_region,
    )


# Memoized bucket-ensure state. Verifying the bucket + CORS once per process is
# enough on the single-instance deployment — without this, EVERY presign would
# re-run head_bucket + put_bucket_cors (two network round-trips, plus a logged
# warning on every request for providers that don't support bucket-level CORS
# via the S3 API). A provider-side CORS change needs a redeploy to take effect
# here; that is acceptable for a bootstrap that is only meant to make the first
# upload work.
_bucket_ensured = False


def _ensure_bucket(s3) -> None:
    """Idempotently ensure the storage bucket exists and allows browser CORS
    access. Best-effort, never fatal, and memoized per process.

    The presigned-PUT upload path ships the file STRAIGHT from the browser to
    storage, so the bucket must answer the browser's CORS preflight — without
    an allow-PUT rule the browser blocks every direct upload before a byte is
    sent. Some providers don't support bucket-level CORS via the S3 API
    (Supabase Storage, MinIO) — the failure is logged once and skipped, and
    the bucket can be configured in the provider dashboard instead."""
    global _bucket_ensured
    if _bucket_ensured:
        return
    try:
        try:
            s3.head_bucket(Bucket=settings.minio_bucket)
        except Exception:
            s3.create_bucket(Bucket=settings.minio_bucket)
        _bucket_ensured = True
    except Exception as e:
        logger.warning("Bucket ensure failed for %s: %s", settings.minio_bucket, e)
        return
    try:
        s3.put_bucket_cors(
            Bucket=settings.minio_bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        # Writes are only possible with a valid presigned URL, so
                        # allowing any origin is safe — this rule is what lets the
                        # uploader's browser PUT (and every embed's GET) work.
                        # Range is REQUIRED for the webseed: the magnet's ws=
                        # makes WebTorrent fetch pieces via HTTP Range requests
                        # (BEP 19), and a browser cross-origin Range request is
                        # preflighted — without the Range header allow-list the
                        # webseed silently never engages.
                        "AllowedOrigins": ["*"],
                        "AllowedMethods": ["GET", "HEAD", "PUT"],
                        "AllowedHeaders": ["Content-Type", "Range"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )
    except Exception as e:
        logger.warning(
            "Bucket CORS bootstrap skipped for %s: %s", settings.minio_bucket, e
        )


def _upload_response_from_row(row) -> UploadResponse:
    """Build the upload response from an uploads row (used for idempotent retries)."""
    return UploadResponse(
        file_id=row["id"],
        filename=row["filename"],
        minio_url=row["minio_url"],
        magnet_link=row["torrent_magnet"],
        file_size_mb=row["file_size_mb"],
        embed_url=f"{settings.app_url}/embed/{row['id']}",
        thumbnail_url=row["thumbnail_url"],
    )


async def _find_completed_by_key(pool, user_id: UUID, key: str):
    """Look up a COMPLETED upload for (user, idempotency_key) — the dedup hit."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, filename, minio_url, torrent_magnet, file_size_mb, thumbnail_url "
            "FROM uploads WHERE user_id = $1 AND idempotency_key = $2",
            user_id,
            key,
        )


async def _dedup_content(
    pool, s3, content_hash: str, object_key: str, minio_http_url: str, file_id: str
) -> str:
    """Content-hash dedup: if byte-identical content is ALREADY stored, delete
    the freshly-written object and point this upload at the existing copy.

    Every upload's SHA-256 is computed in the same single pass as the torrent
    piece hashes, so dedup costs one indexed SELECT. Returns the minio_url to
    persist: the existing copy's URL on a hit, the upload's own URL otherwise.
    Best-effort and fail-open:
      - a DB hiccup keeps the fresh object and skips dedup;
      - a failed delete of the duplicate keeps the fresh object TOO (the row
        points at it, so it is referenced — never a permanent orphan; the
        sweep would not reclaim it because this upload's row exists with id ==
        the key's 2nd segment)."""
    if not content_hash:
        return minio_http_url
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT minio_url FROM uploads WHERE content_hash = $1 AND id <> $2 "
                "ORDER BY created_at LIMIT 1",
                content_hash,
                uuid.UUID(file_id),
            )
    except Exception:
        return minio_http_url  # fail-open: keep the fresh object, skip dedup
    if not row:
        return minio_http_url
    existing_url = row["minio_url"]
    try:
        s3.delete_object(Bucket=settings.minio_bucket, Key=object_key)
    except Exception as e:
        # Keep THIS object as the upload's own copy rather than orphaning it
        # forever (its row is about to reference it).
        logger.warning(
            "Content dedup: failed to delete duplicate %s: %s", object_key, e
        )
        return minio_http_url
    logger.info(
        "Content dedup: upload %s reuses stored copy %s (sha256 %s…)",
        file_id,
        existing_url,
        content_hash[:12],
    )
    return existing_url


async def _check_usage_limit(
    pool, user_id: UUID, month: str, file_size_mb: float
) -> tuple:
    """Authoritative plan-limit check for a NEW file about to be accounted.
    Returns (limit_gb, used_gb); raises 402 when the file would exceed the cap."""
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT plan, referral_bonus_gb FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        plan_limits = {"free": 100, "pro": 1024, "business": float("inf")}
        limit_gb = plan_limits.get(user["plan"], 100) + (
            user.get("referral_bonus_gb") or 0
        )

        usage = await conn.fetchrow(
            "SELECT total_gb_used FROM usage_records WHERE user_id = $1 AND month = $2",
            user_id,
            month,
        )
        used_gb = usage["total_gb_used"] if usage else 0
        if used_gb + file_size_mb / 1024 > limit_gb:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Usage limit exceeded ({limit_gb}GB/month). Please upgrade your plan.",
            )
        return limit_gb, used_gb


async def _check_upload_rate_limit(request: Request) -> None:
    """Per-IP upload rate limit (Redis primary, in-memory fallback). Counts
    uploads whose bytes are ABOUT to be stored — a usage rejection (402) never
    reaches it, and it must run AFTER the idempotency dedup so a retry after a
    lost response doesn't consume the budget (F-05 hardening). Validation
    rejections AFTER the browser's PUT has already landed (presigned finalize
    400s) DO consume it — the bytes hit storage either way."""
    ip = get_client_ip(request)
    redis = await get_redis()
    if redis is not None:
        rate_key = f"upload_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= settings.rate_limit_uploads:
            raise HTTPException(status_code=429, detail=_upload_limit_detail())
        await redis.incr(rate_key)
        await redis.expire(rate_key, settings.rate_limit_uploads_window)
    else:
        now = time.time()
        _prune_upload_requests(now)
        recent = [
            t
            for t in _upload_requests.get(ip, [])
            if now - t < settings.rate_limit_uploads_window
        ]
        if len(recent) >= settings.rate_limit_uploads:
            raise HTTPException(status_code=429, detail=_upload_limit_detail())
        recent.append(now)
        _upload_requests[ip] = recent


async def _check_upload_rate_limit_peek(request: Request) -> None:
    """Non-consuming pre-flight for expensive SERVER-SIDE work: raises 429 when
    the per-IP upload budget is already exhausted, WITHOUT incrementing it (the
    authoritative consumption happens later inside _store_and_finalize).
    Import-by-URL calls this before the server-side download, so a user can't
    queue many concurrent multi-GB fetches before any budget is checked."""
    ip = get_client_ip(request)
    redis = await get_redis()
    if redis is not None:
        rate_key = f"upload_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= settings.rate_limit_uploads:
            raise HTTPException(status_code=429, detail=_upload_limit_detail())
    else:
        now = time.time()
        _prune_upload_requests(now)
        recent = [
            t
            for t in _upload_requests.get(ip, [])
            if now - t < settings.rate_limit_uploads_window
        ]
        if len(recent) >= settings.rate_limit_uploads:
            raise HTTPException(status_code=429, detail=_upload_limit_detail())


def _generate_thumbnail(
    s3, temp_path: str, ext: str, user_id: UUID, file_id: str
) -> str | None:
    """Best-effort ffmpeg thumbnail for a video file (never fails the upload).
    Returns the public URL, or None on any failure. FAST SEEK: `-ss` BEFORE
    `-i` jumps to ~2s via the container's keyframes instead of decoding every
    frame from the start (the old `-ss` after `-i` decoded the whole head of a
    long video — a multi-second thumbnail on Render's nano tier)."""
    if ext not in VIDEO_EXTS or not os.path.exists(temp_path):
        return None
    try:
        thumb_path = f"/tmp/{file_id}_thumb.jpg"
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "00:00:02",
                "-i",
                temp_path,
                "-frames:v",
                "1",
                "-vf",
                "scale=640:360",
                thumb_path,
            ],
            capture_output=True,
            timeout=60,
            check=False,  # ffmpeg failure is handled via returncode below
        )
        if proc.returncode == 0 and os.path.exists(thumb_path):
            thumb_key = f"{user_id}/{file_id}/thumbnail.jpg"
            with open(thumb_path, "rb") as tf:
                s3.put_object(
                    Bucket=settings.minio_bucket,
                    Key=thumb_key,
                    Body=tf.read(),
                    ContentType="image/jpeg",
                )
            thumbnail_url = f"{settings.minio_public_url}/{thumb_key}"
            os.remove(thumb_path)
            return thumbnail_url
        logger.warning(
            "ffmpeg thumbnail failed for %s (rc=%s)", temp_path, proc.returncode
        )
    except Exception as e:
        logger.warning("Thumbnail generation skipped for %s: %s", temp_path, e)
    return None


def _torrent_and_thumbnail(
    file_stream, safe_filename: str, user_id: UUID, file_id: str, s3
) -> tuple:
    """Generate the magnet link, (best-effort) video thumbnail, and the file's
    SHA-256 content hash from a seekable file-like object. The stream is
    written ONCE to a temp file whose basename == safe_filename (so the torrent
    name — and therefore the magnet's dn and the reproducible infohash — stays
    correct), and the SHA-1 piece hashes + SHA-256 are computed WHILE writing,
    so the file is never re-read for hashing. Returns
    (magnet_link, thumbnail_url, content_hash); the temp file is always
    cleaned up.

    The infohash comes from torrent_builder.build_magnet(), which reproduces
    torrentool's info-dict bencode byte-for-byte (pinned by
    TestStreamingTorrentBuilder.test_streaming_infohash_byte_identical_to_torrentool),
    and the frontend seeder rebuilds the identical info dict from the magnet —
    so the browser seeds the same swarm the magnet describes. The content hash
    drives storage dedup (identical bytes store once, regardless of filename).
    """
    ext = os.path.splitext(safe_filename)[1].lower()
    temp_dir = None
    magnet_link = None
    thumbnail_url = None
    content_hash = None
    try:
        # The temp file's basename becomes the torrent's `name`. Writing it as
        # `safe_filename` inside a fresh temp DIR — rather than mkstemp, which
        # prepends a random prefix — keeps the torrent name == the real
        # filename: the magnet's `dn` matches the info dict, and the infohash
        # is reproducible from the file bytes alone (a browser P2P seeder
        # rebuilds the identical info dict, so it can seed the same swarm).
        temp_dir = tempfile.mkdtemp(prefix="torrentcd-")
        temp_path = os.path.join(temp_dir, safe_filename)
        # Learn the size with a cheap seek (no data I/O) so piece boundaries
        # are exact, then hash the bytes as they stream to disk — one read.
        file_stream.seek(0, 2)
        file_size = file_stream.tell()
        file_stream.seek(0)

        sha256 = hashlib.sha256()
        with open(temp_path, "wb") as tf:

            def reader():
                chunk = file_stream.read(CHUNK_SIZE)
                if chunk:
                    tf.write(chunk)
                    sha256.update(chunk)
                return chunk

            pieces = hash_pieces_from_stream(reader, file_size)
        content_hash = sha256.hexdigest()

        magnet_link = build_magnet(safe_filename, pieces, file_size)
        thumbnail_url = _generate_thumbnail(s3, temp_path, ext, user_id, file_id)
    except Exception as e:
        logger.error("Torrent generation failed for %s: %s", safe_filename, e)
        magnet_link = f"magnet:?xt=urn:btih:{file_id}&dn={safe_filename}"
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
    return magnet_link, thumbnail_url, content_hash


async def _insert_upload_record(
    pool,
    user_id: UUID,
    file_id: str,
    safe_filename: str,
    minio_http_url: str,
    magnet_link: str,
    file_size_mb: float,
    thumbnail_url,
    idempotency_key: str | None,
    month: str,
    limit_gb: float,
    used_gb: float,
    content_hash: str | None = None,
) -> UploadResponse:
    """Persist the completed upload + usage accounting, resolving same-key
    insert races via the partial unique index on (user_id, idempotency_key),
    and fire 50/80/95% usage-alert notifications. Returns the UploadResponse."""
    try:
        async with pool.acquire() as conn:
            await conn.fetchrow(
                """INSERT INTO uploads (id, user_id, filename, minio_url, torrent_magnet, file_size_mb, thumbnail_url, idempotency_key, content_hash)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   RETURNING id""",
                uuid.UUID(file_id),
                user_id,
                safe_filename,
                minio_http_url,
                magnet_link,
                round(file_size_mb, 2),
                thumbnail_url,
                idempotency_key or None,
                content_hash,
            )

            await conn.execute(
                """INSERT INTO usage_records (user_id, month, total_gb_used)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (user_id, month)
                   DO UPDATE SET total_gb_used = usage_records.total_gb_used + $3""",
                user_id,
                month,
                round(file_size_mb / 1024, 4),
            )
    except UniqueViolationError:
        # A concurrent request with the same key won the insert race — return
        # its completed upload instead of failing (the partial unique index on
        # (user_id, idempotency_key) guarantees at most one record per key, and
        # NULL keys can never violate it).
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, filename, minio_url, torrent_magnet, file_size_mb, thumbnail_url "
                "FROM uploads WHERE user_id = $1 AND idempotency_key = $2",
                user_id,
                idempotency_key,
            )
        if existing:
            logger.info(
                "Upload idempotency race resolved for key %s (user %s)",
                idempotency_key,
                user_id,
            )
            return _upload_response_from_row(existing)
        raise

    # --- Usage alerts: fire 50/80/95% notifications once per month per user ---
    try:
        new_used_gb = used_gb + round(file_size_mb / 1024, 4)
        await check_usage_thresholds(pool, user_id, new_used_gb, limit_gb)
    except Exception as e:
        logger.warning("Usage notification check failed for user %s: %s", user_id, e)

    embed_url = f"{settings.app_url}/embed/{file_id}"

    return UploadResponse(
        file_id=uuid.UUID(file_id),
        filename=safe_filename,
        minio_url=minio_http_url,
        magnet_link=magnet_link,
        file_size_mb=round(file_size_mb, 2),
        embed_url=embed_url,
        thumbnail_url=thumbnail_url,
    )


async def check_usage_thresholds(
    pool, user_id: UUID, used_gb: float, limit_gb: float
) -> None:
    """Insert usage-alert notifications when usage crosses 50/80/95% of the plan limit.

    Each threshold fires at most once per month per user — we check the
    notifications table for an existing notification of the same type in the
    current month before inserting.
    """
    if limit_gb == float("inf") or limit_gb <= 0:
        return  # Unlimited plan (business) — nothing to alert on

    async with pool.acquire() as conn:
        for pct, ntype, message in USAGE_THRESHOLDS:
            if used_gb < limit_gb * pct:
                continue
            # Already fired this month?
            existing = await conn.fetchrow(
                """SELECT 1 FROM notifications
                   WHERE user_id = $1 AND type = $2
                     AND created_at >= date_trunc('month', NOW())
                   LIMIT 1""",
                user_id,
                ntype,
            )
            if existing:
                continue
            await conn.execute(
                "INSERT INTO notifications (user_id, type, message) VALUES ($1, $2, $3)",
                user_id,
                ntype,
                message,
            )
            logger.info("Usage alert %s inserted for user %s", ntype, user_id)


@router.post("", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    request: Request = None,
    user_id: UUID = Depends(get_current_user_id),
    idempotency_key: str | None = Form(None),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if idempotency_key and len(idempotency_key) > 64:
        raise HTTPException(
            status_code=400, detail="idempotency_key too long (max 64 chars)"
        )

    # Sanitize filename — replace dangerous characters
    safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(file.filename))

    # Validate by file extension
    ext = os.path.splitext(safe_filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    # --- Validate Content-Type (but don't reject on it alone) ---
    content_type = file.content_type if hasattr(file, "content_type") else None
    ct_valid, ct_msg = validate_content_type(content_type, safe_filename)
    if not ct_valid:
        raise HTTPException(status_code=400, detail=ct_msg)

    # --- Validate magic bytes BEFORE reading full size ---
    content_valid, content_msg = validate_file_magic(file.file, safe_filename)
    if not content_valid:
        raise HTTPException(status_code=400, detail=content_msg)

    # --- Check for zip bombs (extreme compression ratios) ---
    ratio = estimate_compression_ratio(file.file)
    if ratio and ratio > MAX_COMPRESSION_RATIO:
        raise HTTPException(
            status_code=400,
            detail=f"File rejected: compression ratio ({ratio:.0f}x) exceeds maximum allowed ({MAX_COMPRESSION_RATIO}x). Possible archive bomb.",
        )

    # --- Check file size ---
    file.file.seek(0, 2)
    total_size = file.file.tell()
    file.file.seek(0)

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if total_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size is {settings.max_upload_size_mb}MB",
        )

    file_size_mb = total_size / (1024 * 1024)
    month = datetime.now(UTC).strftime("%Y-%m")

    pool = await get_db_pool()
    return await _store_and_finalize(
        pool,
        user_id,
        safe_filename,
        file_size_mb,
        month,
        file.file,
        idempotency_key,
        request,
    )


async def _alert_admins_malware(
    pool, user_id: UUID, filename: str, file_stream=None
) -> None:
    """Best-effort alert when VirusTotal flags a file as malicious.

    Inserts a notification for every admin user so they can investigate the
    uploading account. Also logs the event at WARNING level for operational
    monitoring. Never raises — a failed notification must never block the
    upload rejection.
    """
    try:
        # Compute content hash for the log (reuse file_stream if seekable,
        # otherwise skip — the hash is informational, not critical).
        content_hash = "unknown"
        if file_stream is not None:
            try:
                pos = file_stream.tell()
                file_stream.seek(0)
                h = hashlib.sha256()
                while chunk := file_stream.read(1 << 20):
                    h.update(chunk)
                content_hash = h.hexdigest()[:16]
                file_stream.seek(pos)
            except Exception:
                pass

        logger.warning(
            "MALWARE DETECTED: user=%s filename='%s' sha256=%s action=rejected+notified",
            user_id,
            filename,
            content_hash,
        )

        async with pool.acquire() as conn:
            # Find all admin user IDs
            admins = await conn.fetch("SELECT id FROM users WHERE is_admin = TRUE")
            if not admins:
                return
            for admin in admins:
                # De-duplicate: skip if an unread malware alert for this user+file
                # already exists (e.g. presigned finalize re-scanned after storage)
                existing = await conn.fetchrow(
                    "SELECT 1 FROM notifications WHERE user_id = $1 AND type = 'malware_detected' AND message LIKE $2 LIMIT 1",
                    admin["id"],
                    f"%{filename}%",
                )
                if existing:
                    continue
                await conn.execute(
                    "INSERT INTO notifications (user_id, type, message) VALUES ($1, $2, $3)",
                    admin["id"],
                    "malware_detected",
                    f"Malware detected: '{filename}' uploaded by user {str(user_id)[:8]}... (hash prefix: {content_hash}) — file was rejected and not stored.",
                )
        logger.info("Malware alert sent to %d admin(s)", len(admins))
    except Exception as e:
        logger.warning("Failed to send malware alert: %s", e)


async def _store_and_finalize(
    pool,
    user_id: UUID,
    safe_filename: str,
    file_size_mb: float,
    month: str,
    file_stream,
    idempotency_key: str | None,
    request: Request,
) -> UploadResponse:
    """Shared post-validation pipeline for the direct upload route and the
    chunked-upload finalize step: idempotency dedup, usage check, per-IP upload
    rate limit, MinIO multipart upload, torrent/thumbnail generation, DB insert,
    and usage alerts. Reads from ``file_stream`` (a seekable file-like object)."""
    # --- Idempotent retry (the frontend re-sends the same key when it retries
    # a transient network failure). If a previous attempt with this key already
    # completed, return its result instead of storing a duplicate. Checked
    # BEFORE the usage/rate-limit gates so a retry neither double-counts
    # bandwidth nor consumes the upload rate-limit budget. ---
    if idempotency_key:
        existing = await _find_completed_by_key(pool, user_id, idempotency_key)
        if existing:
            logger.info(
                "Upload idempotency hit for key %s (user %s)", idempotency_key, user_id
            )
            return _upload_response_from_row(existing)

    # --- Check usage limit ---
    limit_gb, used_gb = await _check_usage_limit(pool, user_id, month, file_size_mb)

    # --- Per-IP upload rate limit (default 10/hour) ---
    # Counts uploads that are ABOUT to hit storage — validation/usage rejections
    # (400/413/402) do not consume the budget, so a user debugging an oversized
    # or mis-typed file can't lock themselves out of uploads (F-05 hardening).
    # The global limiter still paces all request traffic.
    await _check_upload_rate_limit(request)

    # --- Opt-in malware scan (VIRUSTOTAL_API_KEY; fail-open when unconfigured
    # or unreachable — a scanner outage must never block legitimate uploads).
    # Runs BEFORE the bytes hit storage, so a definitively-malicious file is
    # rejected without ever being stored.
    if not await scan_file(file_stream, int(file_size_mb * 1024 * 1024)):
        await _alert_admins_malware(pool, user_id, safe_filename, file_stream)
        raise HTTPException(
            status_code=422,
            detail="File failed malware scanning and was not stored",
        )

    # --- Chunked multipart upload to MinIO ---
    file_id = str(uuid.uuid4())
    minio_key = f"{user_id}/{file_id}/{safe_filename}"

    try:
        s3 = get_minio_client()
        # Ensure bucket exists + CORS for the browser-PUT path (best-effort).
        _ensure_bucket(s3)

        # Initiate multipart upload with an explicit, extension-derived ContentType
        # so the stored object is never served as a sniffed/ambiguous type
        content_type = (
            mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
        )
        mpu = s3.create_multipart_upload(
            Bucket=settings.minio_bucket,
            Key=minio_key,
            ContentType=content_type,
        )
        upload_id = mpu["UploadId"]

        parts = []
        part_number = 1
        while True:
            chunk = file_stream.read(CHUNK_SIZE)
            if not chunk:
                break
            part = s3.upload_part(
                Bucket=settings.minio_bucket,
                Key=minio_key,
                PartNumber=part_number,
                UploadId=upload_id,
                Body=chunk,
            )
            parts.append({"PartNumber": part_number, "ETag": part["ETag"]})
            part_number += 1

        # Complete multipart upload
        s3.complete_multipart_upload(
            Bucket=settings.minio_bucket,
            Key=minio_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

        # MINIO_PUBLIC_URL already includes the bucket path for Supabase Storage
        minio_http_url = f"{settings.minio_public_url}/{minio_key}"
    except Exception as e:
        logger.error("MinIO upload failed for user %s: %s", user_id, e)
        raise HTTPException(
            status_code=500, detail="Storage upload failed. Please try again."
        )

    # --- Generate .torrent, magnet link, thumbnail, and content hash ---
    # CPU/IO-bound (SHA-1 hashing + ffmpeg) — run in a thread so finalize of a
    # big file never freezes the event loop for unrelated requests.
    magnet_link, thumbnail_url, content_hash = await asyncio.to_thread(
        _torrent_and_thumbnail, file_stream, safe_filename, user_id, file_id, s3
    )

    # --- Content dedup: identical bytes store once. The freshly-uploaded
    # object is deleted and the row points at the existing copy. ---
    minio_http_url = await _dedup_content(
        pool, s3, content_hash, minio_key, minio_http_url, file_id
    )

    # --- Webseed (BEP 19): the magnet carries the FINAL public object URL as
    # a ws= param so WebTorrent fetches pieces from origin when the swarm can't
    # supply them (thin/collapsed swarms, first viewers). Appended AFTER dedup
    # because dedup may replace minio_http_url with an existing copy's URL — a
    # pre-dedup URL could point at an object that was just deleted. The ws=
    # param never changes the infohash (info-dict only), so swarm identity is
    # preserved.
    magnet_link = with_webseed(magnet_link, minio_http_url)

    # --- Save to database (+ usage accounting, race resolution, alerts) ---
    return await _insert_upload_record(
        pool,
        user_id,
        file_id,
        safe_filename,
        minio_http_url,
        magnet_link,
        file_size_mb,
        thumbnail_url,
        idempotency_key,
        month,
        limit_gb,
        used_gb,
        content_hash,
    )


# ── Import-by-URL (SSRF-guarded server-side fetch) ──
# The import endpoint makes the SERVER fetch an arbitrary user-supplied URL,
# which is a classic SSRF vector (the backend could be tricked into hitting
# 169.254.169.254 cloud metadata, localhost services, or internal Docker
# networks). The guard below is the minimum a fetch-anything endpoint needs:
# http/https only, and the host must not be — and must not resolve to — a
# private/loopback/link-local/CGNAT address.
#
# DNS REBINDING is CLOSED, not merely mitigated: `_import_pinned_address`
# resolves the hostname exactly once and validates every answer, then the fetch
# goes through `_PinnedHTTPTransport`, whose httpcore network backend connects
# ONLY to the validated IP while the original hostname is still used for the
# Host header and TLS SNI/server-hostname (cert validation is unchanged).
# Nothing re-resolves the hostname at connect time, so a hostile DNS server
# answering a public IP at check time and a private one at connect time gets
# exactly one chance — its private answer is never connected to. The fetch is
# also time-boxed and size-capped so it can't be abused as a long-lived proxy.
_PRIVATE_IP_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # unique-local IPv6
    ipaddress.ip_network("fe80::/10"),  # link-local IPv6
    ipaddress.ip_network("100::/64"),  # discard-only IPv6
]

# Hostname suffixes that must never be imported regardless of what they resolve
# to (defense in depth on top of the IP check — e.g. "metadata" services that
# might not resolve cleanly to a blocked literal).
_IMPORT_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".lan")
IMPORT_MAX_REDIRECTS = 5
# Wall-clock cap for the WHOLE import download (per-request httpx timeouts
# alone don't bound a slow trickle — 1 byte per 29s would stream for hours).
# Enforced with asyncio.timeout around the full fetch+stream.
IMPORT_MAX_TOTAL_SECONDS = 600
IMPORT_USER_AGENT = "TorrentCDN-Import/1.0"


def _import_pinned_address(url: str) -> tuple | None:
    """Resolve + validate the URL's host EXACTLY ONCE and return
    (hostname, pinned_ip) where `pinned_ip` is a PUBLIC address the caller must
    connect to — or None when the URL is unsafe (non-http(s), private/loopback/
    link-local/CGNAT literal, blocked hostname suffix, any private DNS answer,
    or DNS failure). Pinning the returned address — never re-resolving — is
    what closes the DNS-rebinding window. Blocking DNS call; the async caller
    offloads it to a worker thread."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(_IMPORT_BLOCKED_HOST_SUFFIXES):
        return None
    try:
        addr = ipaddress.ip_address(host)  # IP literal → no DNS needed
    except ValueError:
        addr = None
    if addr is not None:
        if any(addr in net for net in _PRIVATE_IP_NETS):
            return None
        return host, str(addr)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        # Any DNS failure (NXDOMAIN, IDNA hostnames, transient OS errors) is
        # treated as unsafe — a guard must fail closed, never 500.
        return None
    seen, public = set(), []
    for info in infos:
        try:
            ip = str(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
        if ip in seen:
            continue
        seen.add(ip)
        if any(ipaddress.ip_address(ip) in net for net in _PRIVATE_IP_NETS):
            return None  # one private answer poisons the whole check
        public.append(ip)
    if not public:
        return None
    return host, public[0]


def _import_url_is_safe(url: str) -> bool:
    """SSRF guard (boolean form, kept for direct unit tests): True only for
    http(s) URLs whose host is not private/loopback/link-local/CGNAT and does
    not resolve to one. `_import_pinned_address` is the real check — it also
    returns the validated IP that the downloader connects to, closing the DNS
    rebinding window."""
    return _import_pinned_address(url) is not None


async def _download_import_url(url: str, dest_path: str, max_bytes: int) -> tuple:
    """Download `url` to `dest_path`, re-running the SSRF guard on EVERY
    redirect hop and enforcing a hard byte cap + wall-clock time budget
    (asyncio.timeout around the whole fetch, so a slow trickle can't stream
    for hours). Each hop resolves + validates its host exactly once and fetches
    through _PinnedHTTPTransport, which connects only to the validated IP —
    the hostname is never re-resolved at connect time (DNS rebinding is
    closed). Returns (content_type, total_bytes). Raises HTTPException with a
    friendly message on any failure (unreachable/redirect loop/size/timeout/
    HTTP error)."""
    current = url
    try:
        # DNS resolution is blocking — offload to a worker thread so the event
        # loop is never stalled by a slow resolver.
        async with asyncio.timeout(IMPORT_MAX_TOTAL_SECONDS):
            for _hop in range(IMPORT_MAX_REDIRECTS + 1):
                # Resolve + validate the host EXACTLY ONCE per hop. The
                # transport connects to this pinned address — nothing re-resolves
                # the hostname at connect time, so a DNS-rebinding server that
                # answers public-then-private can never redirect the connect to
                # a private address.
                pinned = await asyncio.to_thread(_import_pinned_address, current)
                if pinned is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Import URL points at a private/internal address and was blocked",
                    )
                _, pinned_ip = pinned
                try:
                    async with httpx.AsyncClient(
                        follow_redirects=False,
                        timeout=httpx.Timeout(30, connect=15, read=30),
                        transport=_PinnedHTTPTransport(pinned_ip),
                    ) as client:
                        resp = await client.get(
                            current, headers={"User-Agent": IMPORT_USER_AGENT}
                        )
                except httpx.TimeoutException:
                    raise HTTPException(status_code=504, detail="Import URL timed out")
                except Exception:
                    raise HTTPException(
                        status_code=502, detail="Could not reach the import URL"
                    )
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise HTTPException(
                            status_code=400,
                            detail="Import URL redirected without a target",
                        )
                    current = str(httpx.URL(current).join(location))
                    continue
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Import URL returned HTTP {resp.status_code}",
                    )
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise HTTPException(
                        status_code=413, detail="Imported file exceeds the size limit"
                    )
                content_type = (
                    (resp.headers.get("content-type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                total = 0
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        total += len(chunk)
                        if total > max_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail="Imported file exceeds the size limit",
                            )
                        f.write(chunk)
                return content_type, total
            # Exhausted every redirect hop without a final response.
            raise HTTPException(
                status_code=400, detail="Import URL redirected too many times"
            )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Import URL timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Import download failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach the import URL")


def _import_filename(user_filename, url: str, content_type: str) -> str:
    """Derive a safe filename for an import: an explicit user filename wins,
    then the URL path's basename, then a Content-Type → extension mapping.
    Returns a sanitized name with a supported extension, or raises 400."""
    candidate = None
    if user_filename and user_filename.strip():
        candidate = os.path.basename(user_filename.strip())
    else:
        path = urlparse(url).path
        if path:
            # Percent-encoded names (my%20video.mp4) decode before sanitizing.
            candidate = unquote(os.path.basename(path))
    if candidate:
        candidate = re.sub(r"[^a-zA-Z0-9._-]", "_", candidate)
        if os.path.splitext(candidate)[1].lower() in ALLOWED_EXTS:
            return candidate
    if content_type:
        for ext, mimes in EXTENSION_MIME_MAP.items():
            if content_type in mimes and ext in ALLOWED_EXTS:
                return f"imported{ext}"
    raise HTTPException(
        status_code=400,
        detail="Could not determine a supported file type from the URL",
    )


@router.post("/import", response_model=UploadResponse)
async def import_from_url(
    body: UploadImportRequest,
    request: Request = None,
    user_id: UUID = Depends(get_current_user_id),
):
    """Import a media file by URL. The server downloads a copy (SSRF-guarded,
    size-capped, time-boxed), then runs the SAME post-validation pipeline as
    every other upload path — magic bytes, compression ratio, malware scan,
    usage gate, per-IP upload budget, multipart store, torrent/thumbnail, and
    content-hash dedup (identical bytes store once regardless of source)."""
    temp_dir = None
    try:
        # Cheap pre-flight: only http(s) is ever fetchable — a garbage string
        # must not fall through to the misleading "private/internal" message,
        # and the scheme check is what makes the SSRF guard's assumptions hold.
        if not body.url.strip() or urlparse(body.url).scheme not in ("http", "https"):
            raise HTTPException(
                status_code=400, detail="Import URL must be an http(s) URL"
            )

        # Peek the per-IP upload budget BEFORE downloading — the download is the
        # expensive server-side part, and the pipeline's budget consumption only
        # happens after the bytes arrive (without this, an authenticated user
        # could queue many concurrent multi-GB fetches before any check).
        await _check_upload_rate_limit_peek(request)

        max_bytes = settings.max_upload_size_mb * 1024 * 1024
        temp_dir = tempfile.mkdtemp(prefix="torrentcd-import-")
        temp_path = os.path.join(temp_dir, "download.bin")

        content_type, total = await _download_import_url(body.url, temp_path, max_bytes)
        if total == 0:
            raise HTTPException(
                status_code=400, detail="Import URL returned an empty file"
            )

        safe_filename = _import_filename(body.filename, body.url, content_type)
        ct_valid, ct_msg = validate_content_type(content_type, safe_filename)
        if not ct_valid:
            raise HTTPException(
                status_code=400, detail=f"Imported file type mismatch: {ct_msg}"
            )

        # Byte-level content validation, mirroring the direct/chunked paths
        # (the shared pipeline does NOT do this — its callers do): magic-byte
        # check + zip-bomb compression-ratio check on the downloaded copy.
        with open(temp_path, "rb") as f:
            content_valid, content_msg = validate_file_magic(f, safe_filename)
            if not content_valid:
                raise HTTPException(
                    status_code=400, detail=f"Imported file rejected: {content_msg}"
                )
            ratio = estimate_compression_ratio(f)
            if ratio and ratio > MAX_COMPRESSION_RATIO:
                raise HTTPException(
                    status_code=400,
                    detail=f"File rejected: compression ratio ({ratio:.0f}x) exceeds maximum allowed ({MAX_COMPRESSION_RATIO}x). Possible archive bomb.",
                )

        file_size_mb = total / (1024 * 1024)
        month = datetime.now(UTC).strftime("%Y-%m")
        pool = await get_db_pool()
        with open(temp_path, "rb") as f:
            return await _store_and_finalize(
                pool,
                user_id,
                safe_filename,
                file_size_mb,
                month,
                f,
                body.idempotency_key,
                request,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Import failed for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Import failed. Please try again.")
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


@router.post("/session", response_model=UploadSessionResponse)
async def create_upload_session(
    body: UploadSessionRequest,
    user_id: UUID = Depends(get_current_user_id),
):
    """Start a chunked upload (the client-side chunking fallback). Validates the
    filename/size upfront (fail-fast before the client streams chunks), then
    returns an upload_id the client ships ≤4MB chunks to. If the idempotency key
    already has a completed upload (e.g. the direct attempt stored the file but
    the response was lost), returns it directly so the client skips chunks."""
    await _prune_chunk_sessions()

    safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(body.filename))
    ext = os.path.splitext(safe_filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    ct_valid, ct_msg = validate_content_type(body.content_type, safe_filename)
    if not ct_valid:
        raise HTTPException(status_code=400, detail=ct_msg)

    if body.file_size <= 0:
        raise HTTPException(status_code=400, detail="File is empty")
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if body.file_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size is {settings.max_upload_size_mb}MB",
        )
    if body.total_chunks < 1 or body.total_chunks > 1024:
        raise HTTPException(status_code=400, detail="Invalid total_chunks")
    if body.file_size > body.total_chunks * MAX_CLIENT_CHUNK_SIZE:
        raise HTTPException(
            status_code=400,
            detail="total_chunks does not cover the declared file size",
        )

    key = body.idempotency_key
    if key and len(key) > 64:
        raise HTTPException(
            status_code=400, detail="idempotency_key too long (max 64 chars)"
        )

    # Dedup before any accounting: a retry of an upload that already completed
    # (lost response) should return the stored result, not fail or re-upload.
    if key:
        pool = await get_db_pool()
        existing = await _find_completed_by_key(pool, user_id, key)
        if existing:
            logger.info("Chunked session dedup hit for key %s (user %s)", key, user_id)
            return UploadSessionResponse(
                upload_id=None, result=_upload_response_from_row(existing)
            )

    # Fail-fast usage check — finalize re-checks authoritatively via the shared
    # pipeline (the client shouldn't stream hundreds of chunks for a file that
    # can't be stored).
    pool = await get_db_pool()
    month = datetime.now(UTC).strftime("%Y-%m")
    file_size_mb = body.file_size / (1024 * 1024)
    await _check_usage_limit(pool, user_id, month, file_size_mb)

    # Cap concurrent GROWABLE chunk sessions per user (bounds temp-disk usage).
    # See the constant's docstring: fully-received sessions can't grow, so they
    # don't count toward the cap (avoiding false 429s for legit power users).
    await _prune_chunk_sessions()
    active = await _active_chunk_sessions(user_id)
    if len(active) >= MAX_ACTIVE_CHUNK_SESSIONS_PER_USER:
        raise HTTPException(
            status_code=429,
            detail="Too many active upload sessions. Finalize or wait for them to expire.",
        )

    upload_id = str(uuid.uuid4())
    await _save_chunk_session(
        upload_id,
        {
            "user_id": user_id,
            "filename": safe_filename,
            "total_chunks": body.total_chunks,
            "file_size": body.file_size,
            "idempotency_key": key,
            "received": set(),
            "path": None,
            "created_at": time.time(),
        },
    )
    return UploadSessionResponse(upload_id=upload_id, result=None)


@router.post("/chunk/{upload_id}/{index}", response_model=ChunkUploadResponse)
async def upload_chunk(
    upload_id: str,
    index: int,
    chunk: UploadFile = File(...),
    user_id: UUID = Depends(get_current_user_id),
):
    """Append one ≤4MB chunk to the session's assembly file. Chunks may arrive
    out of order and be re-sent safely (offset writes are idempotent) — the
    frontend retries chunks through the same-origin Vercel proxy on transient
    failures."""
    await _prune_chunk_sessions()
    session = await _load_chunk_session(upload_id)
    if session is None or session["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Upload session not found")
    if index < 0 or index >= session["total_chunks"]:
        raise HTTPException(status_code=400, detail="Invalid chunk index")

    # Cap memory: read at most one byte more than the allowed chunk size.
    data = await chunk.read(MAX_CLIENT_CHUNK_SIZE + 1)
    if len(data) > MAX_CLIENT_CHUNK_SIZE:
        raise HTTPException(status_code=400, detail="Chunk too large")

    # Lazily create the assembly file on the first chunk.
    if session.get("path") is None:
        fd, path = tempfile.mkstemp(suffix=f"_{session['filename']}")
        os.close(fd)
        session["path"] = path

    # Cap disk: a chunk must not extend the assembly file past the declared
    # size. Without this, a session declaring a small file_size with a large
    # total_chunks could grow a temp file far beyond the plan limit (offsets
    # are bounded only by total_chunks). The legit client slices the file into
    # MAX_CLIENT_CHUNK_SIZE pieces, so the last chunk is smaller and this
    # bound holds exactly.
    if index * MAX_CLIENT_CHUNK_SIZE + len(data) > session["file_size"]:
        raise HTTPException(status_code=400, detail="Chunk exceeds declared file size")

    with open(session["path"], "r+b") as f:
        f.seek(index * MAX_CLIENT_CHUNK_SIZE)
        f.write(data)

    session["received"].add(index)
    await _save_chunk_session(upload_id, session)
    return ChunkUploadResponse(
        received=len(session["received"]), total=session["total_chunks"]
    )


@router.post("/finalize/{upload_id}", response_model=UploadResponse)
async def finalize_upload(
    upload_id: str,
    request: Request = None,
    user_id: UUID = Depends(get_current_user_id),
):
    """Assemble + validate the chunked file, then run the shared storage pipeline
    (MinIO, torrent/thumbnail, DB). Idempotent: the session is only removed by
    the TTL prune, and _store_and_finalize dedupes on the session's idempotency
    key — so a retry of finalize after a lost response returns the stored upload
    instead of duplicating it."""
    await _prune_chunk_sessions()
    session = await _load_chunk_session(upload_id)
    if session is None or session["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Upload session not found")

    if len(session["received"]) < session["total_chunks"]:
        raise HTTPException(
            status_code=400,
            detail=f"Upload incomplete ({len(session['received'])}/{session['total_chunks']} chunks received)",
        )

    path = session.get("path")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=400, detail="No chunks received")

    try:
        with open(path, "r+b") as f:
            # Final safety validation on the assembled file (same as the direct path).
            content_valid, content_msg = validate_file_magic(f, session["filename"])
            if not content_valid:
                raise HTTPException(status_code=400, detail=content_msg)
            ratio = estimate_compression_ratio(f)
            if ratio and ratio > MAX_COMPRESSION_RATIO:
                raise HTTPException(
                    status_code=400,
                    detail=f"File rejected: compression ratio ({ratio:.0f}x) exceeds maximum allowed ({MAX_COMPRESSION_RATIO}x). Possible archive bomb.",
                )

            f.seek(0, 2)
            total_size = f.tell()
            f.seek(0)
            max_bytes = settings.max_upload_size_mb * 1024 * 1024
            if total_size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max size is {settings.max_upload_size_mb}MB",
                )

            # Trim zero padding left by a partial final chunk.
            if total_size > session["file_size"]:
                f.truncate(session["file_size"])
                total_size = session["file_size"]

            file_size_mb = total_size / (1024 * 1024)
            month = datetime.now(UTC).strftime("%Y-%m")

            pool = await get_db_pool()
            return await _store_and_finalize(
                pool,
                user_id,
                session["filename"],
                file_size_mb,
                month,
                f,
                session.get("idempotency_key"),
                request,
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Chunked upload finalize failed for session %s", upload_id)
        raise HTTPException(
            status_code=500, detail="Storage upload failed. Please try again."
        )


@router.post("/presign", response_model=UploadPresignResponse)
async def presign_upload(
    body: UploadPresignRequest,
    user_id: UUID = Depends(get_current_user_id),
):
    """Issue a short-lived presigned PUT URL so the browser can ship the file
    bytes STRAIGHT to Supabase Storage — never through Render or the Vercel
    proxy (whose ~4.5MB request-body cap would reject any real video). The
    backend only ever sees the tiny presign/finalize JSON; the heavy upload
    path is browser → storage. Idempotent: a completed upload for the key is
    returned directly, and a pending session for the same key is reused (same
    upload_id / object key, fresh URL)."""
    _prune_presign_sessions()

    safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(body.filename))
    ext = os.path.splitext(safe_filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    ct_valid, ct_msg = validate_content_type(body.content_type, safe_filename)
    if not ct_valid:
        raise HTTPException(status_code=400, detail=ct_msg)

    if body.file_size <= 0:
        raise HTTPException(status_code=400, detail="File is empty")
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if body.file_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size is {settings.max_upload_size_mb}MB",
        )

    key = body.idempotency_key
    if key and len(key) > 64:
        raise HTTPException(
            status_code=400, detail="idempotency_key too long (max 64 chars)"
        )

    pool = await get_db_pool()

    # Dedup against COMPLETED uploads (a retry after a lost response returns
    # the stored result and the client skips the PUT entirely).
    if key:
        existing = await _find_completed_by_key(pool, user_id, key)
        if existing:
            logger.info("Presign dedup hit for key %s (user %s)", key, user_id)
            return UploadPresignResponse(result=_upload_response_from_row(existing))

    # Reuse a pending session for the same key (idempotent presign) — same
    # upload_id/object key, just a fresh URL so a near-expired one can't stall
    # the PUT. Checks the in-memory copy first, then the Redis index (a fresh
    # instance whose memory is cold still finds the pending session).
    reuse = await _find_presign_session_by_key(user_id, key) if key else None

    if reuse:
        upload_id, session = reuse
        object_key = session["object_key"]
        content_type = session["content_type"]
    else:
        # Cap concurrent presign sessions per user (bounds Redis/in-memory
        # session accumulation). Counts the in-memory copies; on a fresh
        # instance whose memory is cold, Redis-backed sessions are missed until
        # the next prune — a minor undercount (they self-expire in 30 min) that
        # is bounded by the per-IP upload limiter at finalize anyway.
        _prune_presign_sessions()
        active = [
            s
            for s in _presign_sessions.values()
            if str(s.get("user_id")) == str(user_id)
        ]
        if len(active) >= MAX_ACTIVE_PRESIGN_SESSIONS_PER_USER:
            raise HTTPException(
                status_code=429,
                detail="Too many pending uploads. Finalize them or wait for them to expire.",
            )
        upload_id = str(uuid.uuid4())
        object_key = f"{user_id}/{upload_id}/{safe_filename}"
        content_type = (
            mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
        )

    # Fail-fast usage check — finalize re-checks authoritatively; the client
    # shouldn't PUT a multi-GB body for a file that can't be stored.
    month = datetime.now(UTC).strftime("%Y-%m")
    file_size_mb = body.file_size / (1024 * 1024)
    await _check_usage_limit(pool, user_id, month, file_size_mb)

    s3 = get_minio_client()
    # Bucket must exist and answer CORS before the browser can PUT (best-effort).
    _ensure_bucket(s3)
    # ContentType is a SIGNED header: the browser MUST send exactly this value
    # in its PUT (the frontend echoes presign.content_type back). Signing it
    # guarantees the stored object's type is deterministic and never sniffed.
    presigned_url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": settings.minio_bucket,
            "Key": object_key,
            "ContentType": content_type,
        },
        ExpiresIn=PRESIGN_URL_TTL,
        HttpMethod="PUT",
    )

    await _save_presign_session(
        upload_id,
        {
            "user_id": user_id,
            "filename": safe_filename,
            "file_size": body.file_size,
            "content_type": content_type,
            "idempotency_key": key,
            "object_key": object_key,
            "created_at": time.time(),
        },
    )
    return UploadPresignResponse(
        upload_id=upload_id,
        presigned_url=presigned_url,
        object_key=object_key,
        content_type=content_type,
        expires_in=PRESIGN_URL_TTL,
        result=None,
    )


@router.post("/presign/finalize/{upload_id}", response_model=UploadResponse)
async def presign_finalize(
    upload_id: str,
    request: Request = None,
    user_id: UUID = Depends(get_current_user_id),
):
    """Verify + finalize a presigned direct upload. The browser already PUT the
    object into Supabase Storage; here we stream it back server-to-server to run
    the SAME post-validation pipeline as every other path (magic bytes,
    compression ratio, size), build the torrent/thumbnail, and persist the
    record. Idempotent: dedup on the session's idempotency key before consuming
    the rate-limit budget, and any rejected object is deleted from storage so a
    retry starts clean."""
    _prune_presign_sessions()
    session = await _load_presign_session(upload_id)
    if session is None or str(session.get("user_id")) != str(user_id):
        # Lost-response retry: the first finalize may have COMPLETED — the
        # session is deleted on success — while its response died at the proxy
        # (a Render cold-start signature; the live probe hit exactly this:
        # finalize got a transient 502, the client's retry then 404'd). The
        # uploads row uses upload_id as its id, so a row for THIS user with
        # id == upload_id proves the upload finalized: return it instead of a
        # 404 so the retry succeeds (same contract as the idempotency-key dedup
        # below, but keyed by the upload_id the client actually holds — it never
        # had the key). Authorization is preserved: the query filters user_id,
        # and a non-UUID upload_id skips the query entirely (a malformed path
        # param must never 500).
        parsed_id = None
        try:
            parsed_id = uuid.UUID(upload_id)
        except ValueError:
            pass
        if parsed_id is not None:
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, filename, minio_url, torrent_magnet, file_size_mb, thumbnail_url "
                    "FROM uploads WHERE id = $1 AND user_id = $2",
                    parsed_id,
                    user_id,
                )
            if row:
                logger.info(
                    "Presign finalize lost-response retry hit for upload %s (user %s)",
                    upload_id,
                    user_id,
                )
                return _upload_response_from_row(row)
        raise HTTPException(status_code=404, detail="Upload session not found")

    pool = await get_db_pool()
    key = session.get("idempotency_key")

    # Idempotent retry of finalize (lost response): return the stored upload
    # without re-consuming the upload rate-limit budget.
    if key:
        existing = await _find_completed_by_key(pool, user_id, key)
        if existing:
            logger.info("Presign finalize dedup hit for key %s (user %s)", key, user_id)
            return _upload_response_from_row(existing)

    # Authoritative usage gate BEFORE the rate limiter (F-05, same as the
    # direct path): the head_object size check below guarantees the stored
    # object equals the declared size, so checking against session["file_size"]
    # here is exact — a plan-limit 402 never burns the hourly upload budget.
    month = datetime.now(UTC).strftime("%Y-%m")
    file_size_mb = session["file_size"] / (1024 * 1024)
    limit_gb, used_gb = await _check_usage_limit(pool, user_id, month, file_size_mb)

    await _check_upload_rate_limit(request)

    object_key = session["object_key"]
    s3 = get_minio_client()
    temp_dir = None
    temp_path = None
    try:
        # Verify the browser's PUT actually landed and matches the declared size
        # (a truncated/corrupt upload is rejected instead of finalized, and a
        # smaller-than-declared file can't dodge the plan limit).
        try:
            head = s3.head_object(Bucket=settings.minio_bucket, Key=object_key)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Upload not found or expired — please retry the upload",
            )
        if head.get("ContentLength") != session["file_size"]:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file size does not match — please retry the upload",
            )

        # Stream the object to a temp file for validation + thumbnail, computing
        # the torrent's SHA-1 piece hashes WHILE the bytes download — the file
        # is never re-read for hashing, and the hashing overlaps the (dominant)
        # network cost of the download. Named as {tmpdir}/{real filename} (NOT
        # mkstemp's random prefix) so the torrent name == the real filename:
        # the magnet's dn and infohash then match what a browser P2P seeder
        # rebuilds (torrent_builder.build_magnet is byte-identical to the old
        # torrentool hashing — see TestStreamingTorrentBuilder).
        def _download_and_hash(path: str, file_size: int) -> tuple:
            # boto3 streaming is sync/blocking — run the whole download in a
            # worker thread so a big file's finalize never stalls the event loop.
            obj = s3.get_object(Bucket=settings.minio_bucket, Key=object_key)
            body = obj["Body"]
            sha256 = hashlib.sha256()
            with open(path, "wb") as tf:

                def reader():
                    chunk = body.read(CHUNK_SIZE)
                    if chunk:
                        tf.write(chunk)
                        sha256.update(chunk)
                    return chunk

                return hash_pieces_from_stream(reader, file_size), sha256.hexdigest()

        temp_dir = tempfile.mkdtemp(prefix="torrentcd-")
        temp_path = os.path.join(temp_dir, session["filename"])
        pieces, content_hash = await asyncio.to_thread(
            _download_and_hash, temp_path, session["file_size"]
        )

        with open(temp_path, "r+b") as f:
            # Final safety validation on the stored bytes (same as the direct
            # and chunked paths — the presign step can't check magic bytes, so
            # this is where a disguised binary is caught).
            content_valid, content_msg = validate_file_magic(f, session["filename"])
            if not content_valid:
                raise HTTPException(status_code=400, detail=content_msg)
            ratio = estimate_compression_ratio(f)
            if ratio and ratio > MAX_COMPRESSION_RATIO:
                raise HTTPException(
                    status_code=400,
                    detail=f"File rejected: compression ratio ({ratio:.0f}x) exceeds maximum allowed ({MAX_COMPRESSION_RATIO}x). Possible archive bomb.",
                )

            f.seek(0, 2)
            total_size = f.tell()
            f.seek(0)
            max_bytes = settings.max_upload_size_mb * 1024 * 1024
            if total_size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max size is {settings.max_upload_size_mb}MB",
                )

            # (The plan-limit gate already ran against the declared size above —
            # the head_object check guarantees actual == declared.)

            # Opt-in malware scan (VIRUSTOTAL_API_KEY; fail-open). The bytes are
            # already in storage (the browser PUT them) — a malicious verdict
            # raises below and the except-HttpException handler deletes the
            # object, so malware never gets a finalized uploads row.
            if not await scan_file(temp_path, total_size):
                await _alert_admins_malware(pool, user_id, session["filename"])
                raise HTTPException(
                    status_code=422,
                    detail="File failed malware scanning",
                )

            minio_http_url = f"{settings.minio_public_url}/{object_key}"
            # Magnet is pure CPU over the already-computed piece hashes (tiny
            # bencode + sha1) — no file I/O. The ffmpeg thumbnail is CPU/IO
            # bound, so it runs in a thread and never blocks the event loop.
            magnet_link = build_magnet(
                session["filename"], pieces, session["file_size"]
            )
            thumbnail_url = await asyncio.to_thread(
                _generate_thumbnail,
                s3,
                temp_path,
                os.path.splitext(session["filename"])[1].lower(),
                user_id,
                upload_id,
            )
            # Content dedup: identical bytes store once — delete the browser's
            # fresh object and point this row at the existing copy.
            minio_http_url = await _dedup_content(
                pool, s3, content_hash, object_key, minio_http_url, upload_id
            )
            # Webseed: the magnet carries the FINAL post-dedup public URL as a
            # ws= param (origin backs up thin swarms via HTTP Range requests;
            # never changes the infohash).
            magnet_link = with_webseed(magnet_link, minio_http_url)
            result = await _insert_upload_record(
                pool,
                user_id,
                upload_id,
                session["filename"],
                minio_http_url,
                magnet_link,
                file_size_mb,
                thumbnail_url,
                key,
                month,
                limit_gb,
                used_gb,
                content_hash,
            )
            # Session complete — drop it from both stores. (The orphan sweep
            # only deletes objects with NO uploads row, so a lingering session
            # could never cause a wrong deletion — this is hygiene so the
            # idempotency-key index stops pointing at a stale session.)
            await _delete_presign_session(upload_id)
            return result
    except HTTPException:
        # Rejected for a real reason — the object in storage is garbage; remove
        # it so a retry (new presign → new object key) starts clean and no
        # orphan accumulates. The session is consumed too, so a later presign
        # with the same idempotency key starts a fresh session/object key
        # instead of reusing a dead one.
        try:
            s3.delete_object(Bucket=settings.minio_bucket, Key=object_key)
        except Exception:
            pass
        await _delete_presign_session(upload_id)
        raise
    except Exception:
        logger.exception("Presign finalize failed for session %s", upload_id)
        raise HTTPException(
            status_code=500, detail="Storage upload failed. Please try again."
        )
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def _extract_upload_id_from_key(key: str) -> UUID | None:
    """Object keys are {user_id}/{upload_id}/{filename} (and thumbnails
    {user_id}/{file_id}/thumbnail.jpg) — the 2nd path segment IS the
    uploads.id, for every upload path. Returns None for non-upload keys."""
    parts = key.split("/")
    if len(parts) < 3:
        return None
    try:
        return uuid.UUID(parts[1])
    except (ValueError, AttributeError):
        return None


async def sweep_orphan_objects() -> int:
    """Delete storage objects that belong to NO finalized upload and NO pending
    presign session, and are older than ORPHAN_GRACE_SECONDS. Returns how many
    objects were deleted.

    This is the single place storage objects get garbage-collected. Covers:
    presign PUTs never finalized (session expired), and objects whose DB insert
    crashed after the storage write. Safety rules:
      - objects with an uploads row (id == key's 2nd segment) are NEVER deleted;
      - objects with a pending presign session are left alone (the browser may
        still be finalizing);
      - objects younger than the grace window are left alone (in-flight
        direct/chunked uploads — multipart parts aren't listed, and the
        storage-write → DB-insert gap is milliseconds, not an hour).
    S3 delete of a missing key is a no-op, so concurrent/duplicate sweeps are
    harmless."""
    _prune_presign_sessions()
    s3 = get_minio_client()
    pool = await get_db_pool()
    now = time.time()
    deleted = 0
    continuation = None

    while deleted < ORPHAN_SWEEP_MAX_DELETIONS:
        try:
            if continuation:
                resp = s3.list_objects_v2(
                    Bucket=settings.minio_bucket, ContinuationToken=continuation
                )
            else:
                resp = s3.list_objects_v2(Bucket=settings.minio_bucket)
        except Exception as e:
            logger.warning("Orphan sweep: bucket listing failed: %s", e)
            break

        for obj in resp.get("Contents", []) or []:
            key = obj.get("Key", "")
            upload_id = _extract_upload_id_from_key(key)
            if upload_id is None:
                continue
            try:
                last_modified = obj["LastModified"].timestamp()
            except Exception:
                continue
            if now - last_modified < ORPHAN_GRACE_SECONDS:
                continue
            # Pending presign session → the browser may still be finalizing.
            # FAIL-SAFE: if the session state can't be read (Redis error), SKIP
            # the object — never delete on ambiguity. (The DB check below also
            # skips on error, so both gates are conservative.)
            try:
                pending = await _load_presign_session(
                    str(upload_id), raise_on_error=True
                )
            except Exception:
                logger.warning(
                    "Orphan sweep: session check failed for %s — skipping", key
                )
                continue
            if pending is not None:
                continue
            # Finalized upload → the uploads row exists (id == key's 2nd segment).
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT 1 FROM uploads WHERE id = $1", upload_id
                    )
            except Exception as e:
                logger.warning("Orphan sweep: DB check failed for %s: %s", key, e)
                continue
            if row:
                continue
            # Content-dedup: other uploads may reference this object via
            # minio_url even though its ORIGINAL row is gone (the owner deleted
            # their upload; the refcount-aware delete paths kept the shared
            # bytes). The id check alone would delete shared bytes still in use
            # — the URL check is the second half of the safety gate.
            try:
                object_url = f"{settings.minio_public_url}/{key}"
                async with pool.acquire() as conn:
                    referenced = await conn.fetchrow(
                        "SELECT 1 FROM uploads WHERE minio_url = $1 LIMIT 1",
                        object_url,
                    )
            except Exception as e:
                logger.warning("Orphan sweep: refcount check failed for %s: %s", key, e)
                continue
            if referenced:
                continue
            try:
                s3.delete_object(Bucket=settings.minio_bucket, Key=key)
                deleted += 1
                logger.info("Orphan sweep: deleted %s", key)
            except Exception as e:
                logger.warning("Orphan sweep: delete failed for %s: %s", key, e)
            if deleted >= ORPHAN_SWEEP_MAX_DELETIONS:
                break

        if not resp.get("IsTruncated") or deleted >= ORPHAN_SWEEP_MAX_DELETIONS:
            break
        continuation = resp.get("NextContinuationToken")

    if deleted:
        logger.info("Orphan sweep: deleted %d orphaned object(s)", deleted)
    return deleted


async def run_orphan_sweep_loop() -> None:
    """Background loop (production only — see main.py lifespan): sweep the
    bucket for orphaned objects every ORPHAN_SWEEP_INTERVAL_SECONDS. Never
    raises — a failed sweep is logged and retried next interval."""
    while True:
        try:
            await asyncio.sleep(ORPHAN_SWEEP_INTERVAL_SECONDS)
            await sweep_orphan_objects()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Orphan sweep loop iteration failed")
