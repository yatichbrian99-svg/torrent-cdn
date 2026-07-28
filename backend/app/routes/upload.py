import uuid
import os
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

settings = get_settings()
router = APIRouter(prefix="/upload", tags=["upload"])


def get_minio_client():
    return boto3.client(
        "s3",
        endpoint_url=f"http://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name="us-east-1",
    )


@router.post("", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    user_id: UUID = Depends(get_current_user_id),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    allowed_types = [
        "video/mp4", "video/webm", "video/ogg", "video/quicktime",
        "audio/mpeg", "audio/mp3", "audio/ogg", "audio/wav", "audio/webm",
    ]
    # Allow more types by checking extension
    ext = os.path.splitext(file.filename)[1].lower()
    allowed_exts = {".mp4", ".webm", ".ogv", ".mov", ".mp3", ".ogg", ".wav", ".m4a", ".avi", ".mkv"}
    if file.content_type not in allowed_types and ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    # Read file content
    file_content = await file.read()
    file_size_mb = len(file_content) / (1024 * 1024)

    # Check usage limits
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT plan FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        plan_limits = {"free": 100, "pro": 1024, "business": float("inf")}
        limit_gb = plan_limits.get(user["plan"], 100)

        month = datetime.utcnow().strftime("%Y-%m")
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

    # Upload to MinIO
    file_id = str(uuid.uuid4())
    minio_key = f"{user_id}/{file_id}/{file.filename}"

    try:
        s3 = get_minio_client()
        # Ensure bucket exists
        try:
            s3.head_bucket(Bucket=settings.minio_bucket)
        except Exception:
            s3.create_bucket(Bucket=settings.minio_bucket)

        s3.put_object(
            Bucket=settings.minio_bucket,
            Key=minio_key,
            Body=file_content,
            ContentType=file.content_type or "application/octet-stream",
        )

        # Use localhost:9000 for browser-accessible MinIO URL
        # (minio:9000 only resolves inside Docker network)
        minio_http_url = f"http://localhost:9000/{settings.minio_bucket}/{minio_key}"
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MinIO upload failed: {str(e)}")        # Generate .torrent and magnet link
    try:
        # Save file temporarily to create torrent
        temp_path = f"/tmp/{file_id}_{file.filename}"
        with open(temp_path, "wb") as f:
            f.write(file_content)

        torrent = Torrent.create(temp_path)
        torrent.announce_urls = [f"{settings.app_url}/announce"]
        torrent_file_path = f"/tmp/{file_id}.torrent"
        torrent.to_file(torrent_file_path)

        # Generate magnet link from info hash
        info_hash = str(torrent.info_hash)
        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={file.filename}"

        # Cleanup temp files
        os.remove(temp_path)
        os.remove(torrent_file_path)
    except Exception:
        # Fallback: use file_id as pseudo-info-hash if torrent generation fails
        magnet_link = f"magnet:?xt=urn:btih:{file_id}&dn={file.filename}"

    # Save to database
    async with pool.acquire() as conn:
        db_file = await conn.fetchrow(
            """INSERT INTO uploads (id, user_id, filename, minio_url, torrent_magnet, file_size_mb)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id""",
            uuid.UUID(file_id),
            user_id,
            file.filename,
            minio_http_url,
            magnet_link,
            round(file_size_mb, 2),
        )

        # Update usage record
        await conn.execute(
            """INSERT INTO usage_records (user_id, month, total_gb_used)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id, month)
               DO UPDATE SET total_gb_used = usage_records.total_gb_used + $3""",
            user_id,
            month,
            round(file_size_mb / 1024, 4),
        )

    embed_url = f"{settings.app_url}/embed/{file_id}"

    return UploadResponse(
        file_id=uuid.UUID(file_id),
        filename=file.filename,
        minio_url=minio_http_url,
        magnet_link=magnet_link,
        file_size_mb=round(file_size_mb, 2),
        embed_url=embed_url,
    )
