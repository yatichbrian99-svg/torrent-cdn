import asyncpg
from redis.asyncio import Redis
import logging
from typing import Optional
from .config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Notifications table DDL — created idempotently at startup (see main.py migrations).
NOTIFICATIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL,
    message TEXT NOT NULL,
    read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""

# Connection pools
pg_pool: Optional[asyncpg.Pool] = None
redis_client: Optional[Redis] = None


async def get_db_pool() -> asyncpg.Pool:
    global pg_pool
    if pg_pool is None:
        pg_pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=2,
            max_size=10,
            # Required for Supabase/PgBouncer which doesn't support prepared statements
            statement_cache_size=0,
        )
    return pg_pool


async def get_redis() -> Optional[Redis]:
    """Get Redis client. Returns None if Redis is unavailable (optional dependency)."""
    global redis_client
    if redis_client is None:
        try:
            redis_client = await Redis.from_url(
                settings.redis_url,
                decode_responses=True,
            )
            await redis_client.ping()
        except Exception as e:
            logger.warning("Redis unavailable — skipping (optional dependency): %s", e)
            redis_client = None
            return None
    return redis_client


async def close_db():
    global pg_pool
    if pg_pool:
        await pg_pool.close()
        pg_pool = None


async def close_redis():
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None
