import asyncio
import logging
import time
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID
import httpx

from ..config import get_settings
from ..database import get_db_pool
from ..auth import get_current_user_id, revoke_user_sessions
from ..models import UserAdminResponse, UpdateUserPlanRequest, AdminAnalyticsResponse
from ..security_events import record as record_event

settings = get_settings()

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# Process start time — surfaced as uptime on the health dashboard.
_ADMIN_START_TIME = time.time()


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


@router.get("/stats")
async def admin_stats(_: UUID = Depends(require_admin)):
    """Platform-wide stats for the admin dashboard."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_files = await conn.fetchval("SELECT COUNT(*) FROM uploads")
        total_bandwidth_gb = await conn.fetchval(
            "SELECT COALESCE(SUM(total_gb_used), 0) FROM usage_records"
        )
        total_p2p_saved_gb = await conn.fetchval(
            "SELECT COALESCE(SUM(bytes_p2p), 0) / (1024.0 * 1024.0 * 1024.0) FROM analytics"
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
        new_users_today = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE created_at::date = CURRENT_DATE"
        )
        new_files_today = await conn.fetchval(
            "SELECT COUNT(*) FROM uploads WHERE created_at::date = CURRENT_DATE"
        )

    return {
        "total_users": total_users or 0,
        "total_files": total_files or 0,
        "total_bandwidth_gb": round(total_bandwidth_gb or 0, 2),
        "total_p2p_saved_gb": round(total_p2p_saved_gb or 0, 3),
        "users_by_plan": {
            "free": free_users or 0,
            "pro": pro_users or 0,
            "business": business_users or 0,
        },
        "new_users_today": new_users_today or 0,
        "new_files_today": new_files_today or 0,
        "revenue_estimate": round((pro_users or 0) * 29 + (business_users or 0) * 99, 2),
    }


@router.get("/users")
async def admin_list_users(
    page: int = 1,
    limit: int = 20,
    _: UUID = Depends(require_admin),
):
    """List users with their usage stats. Paginated envelope: {users, total, page, pages}."""
    page = max(1, page)
    limit = max(1, min(limit, 200))
    offset = (page - 1) * limit
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
        rows = await conn.fetch("""
            SELECT
                u.id, u.email, u.plan, u.verified, u.is_admin, u.created_at,
                COALESCE(ur.total_gb_used, 0) as usage_gb,
                (SELECT COUNT(*) FROM uploads WHERE user_id = u.id) as file_count
            FROM users u
            LEFT JOIN usage_records ur ON ur.user_id = u.id
                AND ur.month = $1
            ORDER BY u.created_at DESC
            LIMIT $2 OFFSET $3
        """, datetime.utcnow().strftime("%Y-%m"), limit, offset)

    pages = max(1, (total + limit - 1) // limit) if total else 1
    return {
        "users": [
            {
                "id": str(row["id"]),
                "email": row["email"],
                "plan": row["plan"],
                "verified": bool(row.get("verified", False)),
                "is_admin": bool(row.get("is_admin", False)),
                "total_files": row["file_count"],
                "total_gb_used": round(row["usage_gb"] or 0, 2),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "pages": pages,
    }


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


async def _admin_update_plan(user_id: UUID, data: UpdateUserPlanRequest):
    """Shared plan-update logic used by both PUT and POST routes."""
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


@router.put("/users/{user_id}/plan")
async def admin_update_plan(
    user_id: UUID,
    data: UpdateUserPlanRequest,
    admin_id: UUID = Depends(require_admin),
):
    """Update a user's subscription plan."""
    result = await _admin_update_plan(user_id, data)
    logger.info("Admin %s changed plan of user %s to %s", admin_id, user_id, data.plan)
    record_event("admin_plan_change", detail=f"{admin_id} -> {user_id} = {data.plan}")
    return result


@router.post("/users/{user_id}/plan")
async def admin_update_plan_post(
    user_id: UUID,
    data: UpdateUserPlanRequest,
    admin_id: UUID = Depends(require_admin),
):
    """Update a user's subscription plan (POST alias)."""
    result = await _admin_update_plan(user_id, data)
    logger.info("Admin %s changed plan of user %s to %s", admin_id, user_id, data.plan)
    record_event("admin_plan_change", detail=f"{admin_id} -> {user_id} = {data.plan}")
    return result


@router.post("/users/{user_id}/toggle-admin")
async def admin_toggle_admin(
    user_id: UUID,
    admin_id: UUID = Depends(require_admin),
):
    """Toggle a user's admin flag."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_admin FROM users WHERE id = $1", user_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        new_status = not row["is_admin"]
        await conn.execute(
            "UPDATE users SET is_admin = $1 WHERE id = $2",
            new_status, user_id,
        )

    # Administrative-action audit log (actor + target + outcome).
    logger.info(
        "Admin %s toggled admin status of user %s to %s",
        admin_id, user_id, new_status,
    )
    record_event("admin_toggle", detail=f"{admin_id} -> {user_id} = {new_status}")

    # Rotate the target user's sessions on a PRIVILEGE change: any token they
    # hold was minted under the old admin flag. revoke_user_sessions bumps the
    # DB security_stamp (authoritative — works without Redis) and the Redis
    # pw_change marker (fast path), so get_current_user_id rejects those tokens
    # (they must log in again to pick up the new role).
    await revoke_user_sessions(user_id)

    return {"detail": "Admin status updated", "is_admin": new_status}


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

    # Administrative-action audit log (actor + target).
    logger.info("Admin %s deleted user %s", admin_id, user_id)
    record_event("admin_delete", detail=f"{admin_id} -> {user_id}")
    return {"detail": "User deleted"}


@router.post("/users/{user_id}/verify")
async def admin_verify_user(user_id: UUID, admin_id: UUID = Depends(require_admin)):
    """Manually verify a user's email. Admin-only."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, verified FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user["verified"]:
            return {"detail": f"User {user['email']} is already verified"}

        await conn.execute(
            "UPDATE users SET verified = TRUE WHERE id = $1", user_id
        )
        logger.info("Admin %s verified user %s (%s)", admin_id, user_id, user["email"])
        record_event("admin_verify", detail=f"{admin_id} -> {user_id}")

    return {"detail": f"User {user['email']} verified successfully"}


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


class _WafProbeCache:
    """Short-TTL cache for the WAF self-probe.

    The probe makes an outbound request through the public edge on every call;
    caching for 60s keeps the dashboard snappy on refresh and avoids hammering
    the app's own rate limiter with probe traffic. Thread-safe via a lock.
    """

    _lock = asyncio.Lock()
    _result = None
    _at = 0.0
    _ttl = 60.0

    @classmethod
    async def get(cls, refresh: bool = False) -> dict:
        now = time.time()
        async with cls._lock:
            if not refresh and cls._result is not None and now - cls._at < cls._ttl:
                return cls._result
            cls._result = await _probe_waf_uncached()
            cls._at = now
            return cls._result


async def _probe_waf_uncached() -> dict:
    """Best-effort self-probe of the edge WAF/security posture.

    The backend sits BEHIND the edge (Vercel/Cloudflare), so it can't see its
    own response headers — but it can make an outbound request through the edge
    to the public origin and read what the edge actually sends. Mirrors
    backend/scripts/check_waf.sh. Uses httpx async (never blocks the event
    loop) with a 4s timeout. Never raises; network failures report "unknown"
    (the dashboard must never break because a probe timed out).
    """
    probe = {"status": "unknown", "detail": "probe failed", "headers": {}}
    url = settings.app_url.rstrip("/") + "/"
    try:
        async with httpx.AsyncClient(timeout=4, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "torrentcdn-health-probe"})
            headers = {k.lower(): v for k, v in resp.headers.items()}
            probe["headers"] = {k: v for k, v in headers.items() if k in (
                "server", "cf-ray", "cf-cache-status", "x-vercel-firewall",
                "x-vercel-protection", "content-security-policy",
                "strict-transport-security", "x-content-type-options",
                "x-frame-options", "referrer-policy", "permissions-policy",
            )}
            if "cf-ray" in headers:
                probe["status"] = "cloudflare"
                probe["detail"] = "Cloudflare proxy detected (cf-ray header)"
            elif "x-vercel-firewall" in headers or "x-vercel-protection" in headers:
                probe["status"] = "vercel-firewall"
                probe["detail"] = "Vercel Firewall headers present"
            elif headers.get("server", "").lower().startswith("vercel"):
                probe["status"] = "vercel"
                probe["detail"] = "Vercel edge — no firewall header observed (managed rules may be off / Hobby plan)"
            else:
                probe["status"] = "unknown"
                probe["detail"] = f"server={headers.get('server', '?')} — no known edge fingerprint"
    except Exception as e:
        probe["detail"] = f"probe failed: {e}"
    return probe


@router.get("/health")
@router.get("/health/status")
async def admin_health(_: UUID = Depends(require_admin)):
    """Security + infrastructure health dashboard payload.

    Live checks (DB / Redis / storage), config-driven security posture, a WAF
    self-probe through the public edge, and the recent security-events feed.
    Admin-only — the same data in a public endpoint would leak operational
    posture to attackers.
    """
    from ..database import get_redis
    from ..security_events import recent as recent_events

    now = time.time()
    checks = {}

    # --- Database ----------------------------------------------------------
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["database"] = {"status": "ok", "detail": "SELECT 1 succeeded"}
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)[:200]}

    # --- Redis -------------------------------------------------------------
    try:
        redis = await get_redis()
        if redis is None:
            checks["redis"] = {"status": "absent", "detail": "not configured / unreachable (fail-open fallbacks active)"}
        else:
            await redis.ping()
            checks["redis"] = {"status": "ok", "detail": "ping ok"}
    except Exception as e:
        checks["redis"] = {"status": "error", "detail": str(e)[:200]}

    # --- Storage -----------------------------------------------------------
    try:
        from .upload import get_minio_client
        s3 = get_minio_client()
        # boto3 is blocking — never stall the event loop on the dashboard.
        await asyncio.to_thread(s3.head_bucket, Bucket=settings.minio_bucket)
        checks["storage"] = {"status": "ok", "detail": f"bucket {settings.minio_bucket} reachable"}
    except Exception as e:
        checks["storage"] = {"status": "error", "detail": str(e)[:200]}

    # --- Security posture (config-driven) ----------------------------------
    posture = [
        {
            "check": "API docs (OpenAPI)",
            "status": "pass" if settings.app_env == "production" and not settings.api_docs_enabled else "warn",
            "detail": "disabled in production" if settings.app_env == "production" and not settings.api_docs_enabled
                      else "enabled — route map exposed",
        },
        {
            "check": "Admin bootstrap (/auth/setup)",
            "status": "pass" if not settings.admin_setup_enabled else "warn",
            "detail": "disabled" if not settings.admin_setup_enabled else "ENABLED — first-admin bootstrap still open",
        },
        {
            "check": "Email verification gate",
            "status": "pass" if settings.email_verification_required else "warn",
            "detail": "required for login" if settings.email_verification_required else "not required",
        },
        {
            "check": "CSP enforcement",
            "status": "pass" if not settings.csp_report_only else "warn",
            "detail": "enforcing" if not settings.csp_report_only else "report-only — violations not blocked",
        },
        {
            "check": "MinIO secret default",
            "status": "warn" if settings.minio_secret_key == "minioadmin" else "pass",
            "detail": "default 'minioadmin'" if settings.minio_secret_key == "minioadmin" else "not default",
        },
        {
            "check": "Paystack key placeholder",
            "status": "warn" if settings.paystack_secret_key in ("sk_test_xxx", "sk_live_xxx") else "pass",
            "detail": "placeholder" if settings.paystack_secret_key in ("sk_test_xxx", "sk_live_xxx") else "set",
        },
        {
            "check": "Malware scan (VirusTotal)",
            "status": "warn" if not settings.virustotal_api_key else "pass",
            "detail": "off (fail-open, magic bytes only)" if not settings.virustotal_api_key else "on (hash lookup + <=32MB scans)",
        },
        {
            "check": "Session revocation (DB security_stamp)",
            "status": "pass",
            "detail": "MFA enable/disable + password reset + admin changes rotate sessions without Redis",
        },
        {
            "check": "Environment",
            "status": "pass" if settings.app_env == "production" else "warn",
            "detail": settings.app_env,
        },
    ]

    waf = await _WafProbeCache.get()
    events = recent_events(50)
    for e in events:
        e["ts"] = int(e["ts"])

    return {
        "generated_at": int(now),
        "uptime_seconds": int(now - _ADMIN_START_TIME),
        "checks": checks,
        "waf": waf,
        "posture": posture,
        "events": events,
    }
