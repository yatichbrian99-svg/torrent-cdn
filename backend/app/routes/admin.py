import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from ..config import get_settings
from ..database import get_db_pool
from ..auth import get_current_user_id
from ..models import UserAdminResponse, UpdateUserPlanRequest, AdminAnalyticsResponse

settings = get_settings()

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


async def require_admin(user_id: UUID = Depends(get_current_user_id)) -> UUID:
    """Dependency that ensures the user is an admin."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT is_admin FROM users WHERE id = $1", user_id
        )
        if not user or not user["is_admin"]:
            raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


@router.get("/users", response_model=list[UserAdminResponse])
async def admin_list_users(_: UUID = Depends(require_admin)):
    """List all users with their usage stats."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                u.id, u.email, u.plan, u.verified, u.is_admin, u.created_at,
                COALESCE(ur.total_gb_used, 0) as usage_gb,
                (SELECT COUNT(*) FROM uploads WHERE user_id = u.id) as file_count
            FROM users u
            LEFT JOIN usage_records ur ON ur.user_id = u.id
                AND ur.month = $1
            ORDER BY u.created_at DESC
        """, datetime.utcnow().strftime("%Y-%m"))

    return [
        UserAdminResponse(
            id=row["id"],
            email=row["email"],
            plan=row["plan"],
            verified=row.get("verified", False),
            is_admin=row.get("is_admin", False),
            created_at=row["created_at"],
            usage_gb=round(row["usage_gb"] or 0, 2),
            file_count=row["file_count"],
        )
        for row in rows
    ]


@router.get("/users/{user_id}", response_model=UserAdminResponse)
async def admin_get_user(user_id: UUID, _: UUID = Depends(require_admin)):
    """Get a single user's details."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                u.id, u.email, u.plan, u.verified, u.is_admin, u.created_at,
                COALESCE(ur.total_gb_used, 0) as usage_gb,
                (SELECT COUNT(*) FROM uploads WHERE user_id = u.id) as file_count
            FROM users u
            LEFT JOIN usage_records ur ON ur.user_id = u.id
                AND ur.month = $1
            WHERE u.id = $2
        """, datetime.utcnow().strftime("%Y-%m"), user_id)

        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        return UserAdminResponse(
            id=row["id"],
            email=row["email"],
            plan=row["plan"],
            verified=row.get("verified", False),
            is_admin=row.get("is_admin", False),
            created_at=row["created_at"],
            usage_gb=round(row["usage_gb"] or 0, 2),
            file_count=row["file_count"],
        )


@router.put("/users/{user_id}/plan")
async def admin_update_plan(
    user_id: UUID,
    data: UpdateUserPlanRequest,
    _: UUID = Depends(require_admin),
):
    """Update a user's subscription plan."""
    VALID_PLANS = {"free", "pro", "business"}
    if data.plan not in VALID_PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET plan = $1 WHERE id = $2",
            data.plan, user_id,
        )
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="User not found")
    return {"detail": f"Plan updated to {data.plan}"}


@router.delete("/users/{user_id}")
async def admin_delete_user(user_id: UUID, admin_id: UUID = Depends(require_admin)):
    """Delete a user and all their data. Admin accounts cannot be deleted."""
    from ..routes.upload import get_minio_client
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Prevent deleting admin accounts
        target_user = await conn.fetchrow(
            "SELECT is_admin FROM users WHERE id = $1", user_id
        )
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        if target_user["is_admin"]:
            raise HTTPException(
                status_code=403,
                detail="Admin accounts cannot be deleted. Demote the user first if needed.",
            )

        # Delete upload files from S3 first
        files = await conn.fetch(
            "SELECT id, minio_url FROM uploads WHERE user_id = $1", user_id
        )
        s3 = get_minio_client()
        for f in files:
            try:
                # URL format: .../bucket/user_id/file_id/filename → key: user_id/file_id/filename
                key = "/".join(f["minio_url"].split("/")[-3:])
                s3.delete_object(Bucket=settings.minio_bucket, Key=key)
            except Exception as e:
                logger.warning("Failed to delete S3 object for file %s: %s", f["id"], e)

        # Delete user (cascades to uploads, analytics, usage_records)
        result = await conn.execute(
            "DELETE FROM users WHERE id = $1", user_id
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="User not found")
    return {"detail": "User deleted"}


@router.get("/analytics", response_model=AdminAnalyticsResponse)
async def admin_analytics(_: UUID = Depends(require_admin)):
    """Get platform-wide analytics."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_files = await conn.fetchval("SELECT COUNT(*) FROM uploads")
        total_storage = await conn.fetchval(
            "SELECT COALESCE(SUM(total_gb_used), 0) FROM usage_records"
        )
        free_users = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE plan = 'free'"
        )
        pro_users = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE plan = 'pro'"
        )
        business_users = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE plan = 'business'"
        )

    return AdminAnalyticsResponse(
        total_users=total_users or 0,
        total_files=total_files or 0,
        total_storage_gb=round(total_storage or 0, 2),
        free_users=free_users or 0,
        pro_users=pro_users or 0,
        business_users=business_users or 0,
    )
