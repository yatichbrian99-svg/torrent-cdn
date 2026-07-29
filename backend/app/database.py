import asyncpg
from redis.asyncio import Redis
import logging
from typing import Optional
from .config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

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
        )
    return pg_pool


async def get_redis() -> Redis:
    global redis_client
    if redis_client is None:
        redis_client = await Redis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        try:
            await redis_client.ping()
        except Exception as e:
            logger.error("Redis ping failed: %s", e)
            redis_client = None
            raise RuntimeError("Redis connection failed") from e
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
