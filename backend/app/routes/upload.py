import uuid
import os
import re
import mimetypes
import subprocess
import tempfile
import logging
import time
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, status
from uuid import UUID
from typing import Optional
import boto3
from botocore.config import Config as BotoConfig
from asyncpg.exceptions import UniqueViolationError
from torrentool.api import Torrent

from ..config import get_settings
from ..database import get_db_pool, get_redis
from ..auth import get_current_user_id
from ..models import (
    UploadResponse,
    UploadSessionRequest,
    UploadSessionResponse,
    ChunkUploadResponse,
    UploadPresignRequest,
    UploadPresignResponse,
)
from ..security import (
    validate_file_magic,
    validate_content_type,
    estimate_compression_ratio,
    MAX_COMPRESSION_RATIO,
    read_magic_bytes,
    get_client_ip,
)

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

logger = logging.getLogger(__name__)

settings = get_settings()
router = APIRouter(prefix="/upload", tags=["upload"])

ALLOWED_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".mp3", ".ogg", ".wav", ".m4a", ".avi", ".mkv"}

VIDEO_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".avi", ".mkv"}

# Usage-alert thresholds: (fraction of plan limit, notification type, message)
USAGE_THRESHOLDS = [
    (0.50, "usage_50", "You've used 50% of your monthly bandwidth. Upgrade to avoid interruption."),
    (0.80, "usage_80", "You've used 80% of your monthly bandwidth. Upgrade now to keep delivering."),
    (0.95, "usage_95", "You're almost out of bandwidth. Upgrade immediately to avoid service interruption."),
]

# In-memory fallback for the per-IP upload rate limit (used when Redis is down)
_upload_requests: dict[str, list[float]] = {}

# Chunked-upload sessions (client-side chunking fallback). Kept in memory — fine
# on Render's single-instance free tier. Sessions expire via _prune_chunk_sessions
# (which also deletes the assembly temp file); finalize never deletes the session
# so a lost-response retry of finalize can re-run safely (idempotency dedup
# returns the stored upload instead of duplicating it).
_chunk_sessions: dict[str, dict] = {}

# Presigned-upload sessions (primary direct-to-storage path). Holds the object
# key + declared size so finalize can verify the browser's PUT without trusting
# client-supplied metadata. Expired via _prune_presign_sessions (no temp files
# on disk, so pruning is just dict cleanup).
_presign_sessions: dict[str, dict] = {}


def _prune_presign_sessions(now: Optional[float] = None) -> None:
    """Drop expired presign sessions AND best-effort delete their object from
    storage. A presigned URL that was PUT but never finalized leaves a REAL
    object in the bucket — a leak the chunked flow doesn't have (it only writes
    to storage at finalize). Deleting a never-uploaded key is a harmless no-op."""
    now = now or time.time()
    expired = [k for k, s in _presign_sessions.items() if now - s["created_at"] > PRESIGN_SESSION_TTL]
    if not expired:
        return
    try:
        s3 = get_minio_client()
        for k in expired:
            s = _presign_sessions.pop(k, None)
            if s and s.get("object_key"):
                try:
                    s3.delete_object(Bucket=settings.minio_bucket, Key=s["object_key"])
                except Exception:
                    pass
    except Exception:
        # Storage unreachable — still drop the in-memory session so the dict
        # can't grow unbounded; the object will be pruned by a later run.
        for k in expired:
            _presign_sessions.pop(k, None)


def _prune_chunk_sessions(now: Optional[float] = None) -> None:
    """Drop expired chunked-upload sessions and their assembly temp files."""
    now = now or time.time()
    expired = [k for k, s in _chunk_sessions.items() if now - s["created_at"] > CHUNK_SESSION_TTL]
    for k in expired:
        s = _chunk_sessions.pop(k)
        path = s.get("path")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _prune_upload_requests(now: float) -> None:
    """Drop keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _upload_requests.items() if not v or now - v[-1] > settings.rate_limit_uploads_window]
    for k in expired:
        del _upload_requests[k]


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


async def _check_usage_limit(pool, user_id: UUID, month: str, file_size_mb: float) -> tuple:
    """Authoritative plan-limit check for a NEW file about to be accounted.
    Returns (limit_gb, used_gb); raises 402 when the file would exceed the cap."""
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT plan, referral_bonus_gb FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        plan_limits = {"free": 100, "pro": 1024, "business": float("inf")}
        limit_gb = plan_limits.get(user["plan"], 100) + (user.get("referral_bonus_gb") or 0)

        usage = await conn.fetchrow(
            "SELECT total_gb_used FROM usage_records WHERE user_id = $1 AND month = $2",
            user_id, month,
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
        recent = [t for t in _upload_requests.get(ip, []) if now - t < settings.rate_limit_uploads_window]
        if len(recent) >= settings.rate_limit_uploads:
            raise HTTPException(status_code=429, detail=_upload_limit_detail())
        recent.append(now)
        _upload_requests[ip] = recent


def _torrent_and_thumbnail(file_stream, safe_filename: str, user_id: UUID, file_id: str, s3) -> tuple:
    """Generate the magnet link and (best-effort) video thumbnail from a
    seekable file-like object. Writes the stream to a temp file for hashing;
    the temp file is always cleaned up. Returns (magnet_link, thumbnail_url).

    NOTE: uses Torrent.create_from() — torrentool's actual factory. The old
    Torrent.create() call never existed in torrentool (latest is 1.2.0), so the
    broad except silently minted fake `btih:<uuid>` magnets on every upload.
    """
    ext = os.path.splitext(safe_filename)[1].lower()
    temp_path = None
    magnet_link = None
    thumbnail_url = None
    try:
        # Write content to temp file for torrent creation
        temp_fd, temp_path = tempfile.mkstemp(suffix=f"_{safe_filename}")
        os.close(temp_fd)
        file_stream.seek(0)
        with open(temp_path, "wb") as tf:
            while True:
                chunk = file_stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                tf.write(chunk)

        torrent = Torrent.create_from(temp_path)
        torrent.announce_urls = [
            "wss://tracker.openwebtorrent.com",
            "wss://tracker.btorrent.xyz",
            "wss://tracker.fastcast.nz",
        ]
        info_hash = str(torrent.info_hash)
        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={safe_filename}"

        # --- Generate thumbnail for video files (best-effort, never fails the upload) ---
        if ext in VIDEO_EXTS and os.path.exists(temp_path):
            try:
                thumb_path = f"/tmp/{file_id}_thumb.jpg"
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", temp_path, "-ss", "00:00:02", "-vframes", "1", "-vf", "scale=640:360", thumb_path],
                    capture_output=True,
                    timeout=60,
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
                else:
                    logger.warning("ffmpeg thumbnail failed for %s (rc=%s)", safe_filename, proc.returncode)
            except Exception as e:
                logger.warning("Thumbnail generation skipped for %s: %s", safe_filename, e)
    except Exception as e:
        logger.error("Torrent generation failed for %s: %s", safe_filename, e)
        magnet_link = f"magnet:?xt=urn:btih:{file_id}&dn={safe_filename}"
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return magnet_link, thumbnail_url


async def _insert_upload_record(
    pool,
    user_id: UUID,
    file_id: str,
    safe_filename: str,
    minio_http_url: str,
    magnet_link: str,
    file_size_mb: float,
    thumbnail_url,
    idempotency_key: Optional[str],
    month: str,
    limit_gb: float,
    used_gb: float,
) -> UploadResponse:
    """Persist the completed upload + usage accounting, resolving same-key
    insert races via the partial unique index on (user_id, idempotency_key),
    and fire 50/80/95% usage-alert notifications. Returns the UploadResponse."""
    try:
        async with pool.acquire() as conn:
            await conn.fetchrow(
                """INSERT INTO uploads (id, user_id, filename, minio_url, torrent_magnet, file_size_mb, thumbnail_url, idempotency_key)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   RETURNING id""",
                uuid.UUID(file_id),
                user_id,
                safe_filename,
                minio_http_url,
                magnet_link,
                round(file_size_mb, 2),
                thumbnail_url,
                idempotency_key or None,
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
            logger.info("Upload idempotency race resolved for key %s (user %s)", idempotency_key, user_id)
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


async def check_usage_thresholds(pool, user_id: UUID, used_gb: float, limit_gb: float) -> None:
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
    idempotency_key: Optional[str] = Form(None),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if idempotency_key and len(idempotency_key) > 64:
        raise HTTPException(status_code=400, detail="idempotency_key too long (max 64 chars)")

    # Sanitize filename — replace dangerous characters
    safe_filename = re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.basename(file.filename))

    # Validate by file extension
    ext = os.path.splitext(safe_filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    # --- Validate Content-Type (but don't reject on it alone) ---
    content_type = file.content_type if hasattr(file, 'content_type') else None
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
    month = datetime.utcnow().strftime("%Y-%m")

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


async def _store_and_finalize(
    pool,
    user_id: UUID,
    safe_filename: str,
    file_size_mb: float,
    month: str,
    file_stream,
    idempotency_key: Optional[str],
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
            logger.info("Upload idempotency hit for key %s (user %s)", idempotency_key, user_id)
            return _upload_response_from_row(existing)

    # --- Check usage limit ---
    limit_gb, used_gb = await _check_usage_limit(pool, user_id, month, file_size_mb)

    # --- Per-IP upload rate limit (default 10/hour) ---
    # Counts uploads that are ABOUT to hit storage — validation/usage rejections
    # (400/413/402) do not consume the budget, so a user debugging an oversized
    # or mis-typed file can't lock themselves out of uploads (F-05 hardening).
    # The global 5 req/10s limiter still paces all request traffic.
    await _check_upload_rate_limit(request)

    # --- Chunked multipart upload to MinIO ---
    file_id = str(uuid.uuid4())
    minio_key = f"{user_id}/{file_id}/{safe_filename}"

    try:
        s3 = get_minio_client()
        # Ensure bucket exists with private policy
        try:
            s3.head_bucket(Bucket=settings.minio_bucket)
        except Exception:
            s3.create_bucket(Bucket=settings.minio_bucket)

        # MinIO buckets are private by default — no public access without explicit presigned URLs
        # The bucket policy with aws:PrincipalArn is not supported by MinIO, so we skip it.

        # Initiate multipart upload with an explicit, extension-derived ContentType
        # so the stored object is never served as a sniffed/ambiguous type
        content_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
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
        raise HTTPException(status_code=500, detail="Storage upload failed. Please try again.")

    # --- Generate .torrent and magnet link (+ best-effort thumbnail) ---
    magnet_link, thumbnail_url = _torrent_and_thumbnail(file_stream, safe_filename, user_id, file_id, s3)

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
    )


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
    _prune_chunk_sessions()

    safe_filename = re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.basename(body.filename))
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
        raise HTTPException(status_code=400, detail="idempotency_key too long (max 64 chars)")

    # Dedup before any accounting: a retry of an upload that already completed
    # (lost response) should return the stored result, not fail or re-upload.
    if key:
        pool = await get_db_pool()
        existing = await _find_completed_by_key(pool, user_id, key)
        if existing:
            logger.info("Chunked session dedup hit for key %s (user %s)", key, user_id)
            return UploadSessionResponse(upload_id=None, result=_upload_response_from_row(existing))

    # Fail-fast usage check — finalize re-checks authoritatively via the shared
    # pipeline (the client shouldn't stream hundreds of chunks for a file that
    # can't be stored).
    pool = await get_db_pool()
    month = datetime.utcnow().strftime("%Y-%m")
    file_size_mb = body.file_size / (1024 * 1024)
    await _check_usage_limit(pool, user_id, month, file_size_mb)

    upload_id = str(uuid.uuid4())
    _chunk_sessions[upload_id] = {
        "user_id": user_id,
        "filename": safe_filename,
        "total_chunks": body.total_chunks,
        "file_size": body.file_size,
        "idempotency_key": key,
        "received": set(),
        "path": None,
        "created_at": time.time(),
    }
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
    _prune_chunk_sessions()
    session = _chunk_sessions.get(upload_id)
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

    with open(session["path"], "r+b") as f:
        f.seek(index * MAX_CLIENT_CHUNK_SIZE)
        f.write(data)

    session["received"].add(index)
    return ChunkUploadResponse(received=len(session["received"]), total=session["total_chunks"])


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
    _prune_chunk_sessions()
    session = _chunk_sessions.get(upload_id)
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
            month = datetime.utcnow().strftime("%Y-%m")

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
        raise HTTPException(status_code=500, detail="Storage upload failed. Please try again.")


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

    safe_filename = re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.basename(body.filename))
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
        raise HTTPException(status_code=400, detail="idempotency_key too long (max 64 chars)")

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
    # the PUT.
    reuse = None
    if key:
        for uid, s in _presign_sessions.items():
            if s["user_id"] == user_id and s["idempotency_key"] == key:
                reuse = (uid, s)
                break

    if reuse:
        upload_id, session = reuse
        object_key = session["object_key"]
        content_type = session["content_type"]
    else:
        upload_id = str(uuid.uuid4())
        object_key = f"{user_id}/{upload_id}/{safe_filename}"
        content_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"

    # Fail-fast usage check — finalize re-checks authoritatively; the client
    # shouldn't PUT a multi-GB body for a file that can't be stored.
    month = datetime.utcnow().strftime("%Y-%m")
    file_size_mb = body.file_size / (1024 * 1024)
    await _check_usage_limit(pool, user_id, month, file_size_mb)

    s3 = get_minio_client()
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

    _presign_sessions[upload_id] = {
        "user_id": user_id,
        "filename": safe_filename,
        "file_size": body.file_size,
        "content_type": content_type,
        "idempotency_key": key,
        "object_key": object_key,
        "created_at": time.time(),
    }
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
    session = _presign_sessions.get(upload_id)
    if session is None or session["user_id"] != user_id:
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
    month = datetime.utcnow().strftime("%Y-%m")
    file_size_mb = session["file_size"] / (1024 * 1024)
    limit_gb, used_gb = await _check_usage_limit(pool, user_id, month, file_size_mb)

    await _check_upload_rate_limit(request)

    object_key = session["object_key"]
    s3 = get_minio_client()
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

        # Stream the object to a temp file for validation + torrent/thumbnail.
        obj = s3.get_object(Bucket=settings.minio_bucket, Key=object_key)
        body = obj["Body"]
        fd, temp_path = tempfile.mkstemp(suffix=f"_{session['filename']}")
        os.close(fd)
        with open(temp_path, "wb") as tf:
            while True:
                chunk = body.read(CHUNK_SIZE)
                if not chunk:
                    break
                tf.write(chunk)

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
            minio_http_url = f"{settings.minio_public_url}/{object_key}"
            magnet_link, thumbnail_url = _torrent_and_thumbnail(
                f, session["filename"], user_id, upload_id, s3
            )
            return await _insert_upload_record(
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
            )
    except HTTPException:
        # Rejected for a real reason — the object in storage is garbage; remove
        # it so a retry (new presign → new object key) starts clean and no
        # orphan accumulates.
        try:
            s3.delete_object(Bucket=settings.minio_bucket, Key=object_key)
        except Exception:
            pass
        raise
    except Exception:
        logger.exception("Presign finalize failed for session %s", upload_id)
        raise HTTPException(status_code=500, detail="Storage upload failed. Please try again.")
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
