import uuid
import os
import re
import mimetypes
import subprocess
import tempfile
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from uuid import UUID
import boto3
from botocore.config import Config as BotoConfig
from torrentool.api import Torrent

from ..config import get_settings
from ..database import get_db_pool
from ..auth import get_current_user_id
from ..models import UploadResponse
from ..security import (
    validate_file_magic,
    validate_content_type,
    estimate_compression_ratio,
    MAX_COMPRESSION_RATIO,
    read_magic_bytes,
)

CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB per multipart chunk

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
    user_id: UUID = Depends(get_current_user_id),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

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

    # --- Check usage limit ---
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT plan, referral_bonus_gb FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        plan_limits = {"free": 100, "pro": 1024, "business": float("inf")}
        # Referral bonus GB is added on top of the plan limit.
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
            chunk = await file.read(CHUNK_SIZE)
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

    # --- Generate .torrent and magnet link ---
    temp_path = None
    torrent_file_path = None
    magnet_link = None
    thumbnail_url = None
    try:
        # Write content to temp file for torrent creation
        temp_fd, temp_path = tempfile.mkstemp(suffix=f"_{safe_filename}")
        os.close(temp_fd)
        file.file.seek(0)
        with open(temp_path, "wb") as tf:
            while True:
                chunk = file.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                tf.write(chunk)

        torrent = Torrent.create(temp_path)
        torrent.announce_urls = [
            "wss://tracker.openwebtorrent.com",
            "wss://tracker.btorrent.xyz",
            "wss://tracker.fastcast.nz",
        ]
        torrent_file_path = f"/tmp/{file_id}.torrent"
        torrent.to_file(torrent_file_path)

        info_hash = str(torrent.info_hash)
        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={safe_filename}"

        # --- Generate thumbnail for video files (best-effort, never fails the upload) ---
        if ext in VIDEO_EXTS and temp_path and os.path.exists(temp_path):
            try:
                thumb_path = f"/tmp/{file_id}_thumb.jpg"
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", temp_path, "-ss", "00:00:01", "-vframes", "1", thumb_path],
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
    except Exception:
        magnet_link = f"magnet:?xt=urn:btih:{file_id}&dn={safe_filename}"
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        if torrent_file_path and os.path.exists(torrent_file_path):
            os.remove(torrent_file_path)

    # --- Save to database ---
    async with pool.acquire() as conn:
        await conn.fetchrow(
            """INSERT INTO uploads (id, user_id, filename, minio_url, torrent_magnet, file_size_mb, thumbnail_url)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id""",
            uuid.UUID(file_id),
            user_id,
            safe_filename,
            minio_http_url,
            magnet_link,
            round(file_size_mb, 2),
            thumbnail_url,
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
