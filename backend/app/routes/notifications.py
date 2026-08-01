import logging
from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from ..database import get_db_pool
from ..auth import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(user_id: UUID = Depends(get_current_user_id)):
    """List notifications for the current user, newest first."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, type, message, read, created_at
               FROM notifications
               WHERE user_id = $1
               ORDER BY created_at DESC
               LIMIT 50""",
            user_id,
        )
    return [dict(r) for r in rows]


@router.post("/mark-read/{notification_id}")
async def mark_notification_read(
    notification_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
):
    """Mark a single notification as read."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE notifications SET read = TRUE WHERE id = $1 AND user_id = $2",
            notification_id,
            user_id,
        )
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="Notification not found")
    logger.info("Notification %s marked as read for user %s", notification_id, user_id)
    return {"detail": "Notification marked as read"}
