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

# Users table DDL — full current schema (including columns added by later
# features such as terms_accepted / terms_accepted_at). Created idempotently at
# startup BEFORE the ALTER migrations run so a fresh database gets the complete
# schema on first boot (otherwise the register INSERT referencing these columns
# fails with a 500 on a brand-new DB, and NOTIFICATIONS_TABLE_DDL, which
# REFERENCES users, cannot be created either).
USERS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    plan VARCHAR(50) DEFAULT 'free',
    verified BOOLEAN DEFAULT FALSE,
    is_admin BOOLEAN DEFAULT FALSE,
    stripe_customer_id VARCHAR(255),
    terms_accepted BOOLEAN DEFAULT FALSE,
    terms_accepted_at TIMESTAMP WITH TIME ZONE,
    referral_code VARCHAR(20) UNIQUE,
    referred_by UUID REFERENCES users(id),
    referral_bonus_gb FLOAT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""

# API keys table DDL — long-lived credentials (tcdn_<random>). Only the
# SHA-256 hash of the token is stored (never the plaintext), so a DB leak
# exposes hashes, not usable keys. One active key per user (generate rotates).
API_KEYS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL UNIQUE,
    prefix VARCHAR(24) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE
);
-- Unique per user: enforces the one-active-key invariant AND is the
-- ON CONFLICT target for the atomic rotate upsert in /auth/api-key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
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
