import uuid
import os
import tempfile
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

CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB per multipart chunk

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
    ext = os.path.splitext(file.filename)[1].lower()
    allowed_exts = {".mp4", ".webm", ".ogv", ".mov", ".mp3", ".ogg", ".wav", ".m4a", ".avi", ".mkv"}
    if file.content_type not in allowed_types and ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    # --- Check usage limit (requires Content-Length or stream size) ---
    pool = await get_db_pool()
    file_size_mb = 0
    month = datetime.utcnow().strftime("%Y-%m")

    # Read file size first without reading content
    file.file.seek(0, 2)
    total_size = file.file.tell()
    file.file.seek(0)
    file_size_mb = total_size / (1024 * 1024)

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT plan FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        plan_limits = {"free": 100, "pro": 1024, "business": float("inf")}
        limit_gb = plan_limits.get(user["plan"], 100)

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
    minio_key = f"{user_id}/{file_id}/{file.filename}"

    try:
        s3 = get_minio_client()
        # Ensure bucket exists
        try:
            s3.head_bucket(Bucket=settings.minio_bucket)
        except Exception:
            s3.create_bucket(Bucket=settings.minio_bucket)

        # Initiate multipart upload
        mpu = s3.create_multipart_upload(
            Bucket=settings.minio_bucket,
            Key=minio_key,
            ContentType=file.content_type or "application/octet-stream",
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

        minio_http_url = f"{settings.minio_public_url}/{settings.minio_bucket}/{minio_key}"
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MinIO upload failed: {str(e)}")

    # --- Generate .torrent and magnet link ---
    try:
        # Write content to temp file for torrent creation
        temp_fd, temp_path = tempfile.mkstemp(suffix=f"_{file.filename}")
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
        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={file.filename}"

        os.remove(temp_path)
        os.remove(torrent_file_path)
    except Exception:
        magnet_link = f"magnet:?xt=urn:btih:{file_id}&dn={file.filename}"

    # --- Save to database ---
    async with pool.acquire() as conn:
        await conn.fetchrow(
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
