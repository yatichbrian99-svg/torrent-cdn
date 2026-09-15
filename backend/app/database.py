import logging

import asyncpg
from redis.asyncio import Redis
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError
from redis.retry import Retry

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
    -- Two-factor authentication: the base32 TOTP secret + SHA-256 hashes of the
    -- single-use backup codes (JSON array). Both NULL until setup completes.
    mfa_secret TEXT,
    mfa_enabled BOOLEAN DEFAULT FALSE,
    mfa_backup_codes TEXT,
    -- Session-revocation marker (authoritative, Redis-free): bumped to NOW() on
    -- password reset / MFA enable-disable / admin privilege changes. JWTs whose
    -- iat predates this stamp are rejected, so a pre-change token can never
    -- outlive the change even on deployments without Redis (see auth.py).
    security_stamp TIMESTAMP WITH TIME ZONE,
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

# Payout tables DDL — Paystack Transfers API (admin payouts to M-PESA).
# `payout_recipients` holds the recipients created through the Paystack
# /transferrecipient endpoint (recipient_code is the handle Paystack returns);
# `payouts` records every initiated transfer (transfer_code) and its latest
# status, which the billing webhook keeps in sync via transfer.* events.
# Both reference users(id) with ON DELETE SET NULL so the admin audit trail
# survives account deletion. amount_cents is BIGINT: KES 10M * 100 = 1e9 cents
# exceeds the INT32 ceiling.
PAYOUTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS payout_recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_code VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    account_number VARCHAR(32) NOT NULL,
    bank_code VARCHAR(32) NOT NULL,
    bank_name VARCHAR(255),
    type VARCHAR(32) NOT NULL DEFAULT 'mobile_money',
    currency VARCHAR(8) NOT NULL DEFAULT 'KES',
    active BOOLEAN DEFAULT TRUE,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS payouts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_code VARCHAR(64) NOT NULL UNIQUE,
    recipient_code VARCHAR(64) NOT NULL,
    recipient_name VARCHAR(255),
    amount_cents BIGINT NOT NULL,
    currency VARCHAR(8) NOT NULL DEFAULT 'KES',
    reason TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    reference VARCHAR(64),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payouts_recipient ON payouts(recipient_code);
CREATE INDEX IF NOT EXISTS idx_payouts_created_at ON payouts(created_at DESC);
-- Idempotent initiation: reference doubles as the caller-supplied idempotency
-- key, so a concurrent same-key initiate can't create two rows (the route
-- catches the unique violation and returns the existing row). Paystack
-- references are unique per transfer, so this never collides in normal use.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payouts_reference ON payouts(reference) WHERE reference IS NOT NULL;
"""

# ── Live streaming schema ────────────────────────────────────────────────────
#
# Live channels + durable analytics. EPHEMERAL live state (active viewers,
# peers, heartbeats, per-segment availability) intentionally does NOT live
# here — it is Redis-first with TTLs (see app/live_state.py); Postgres holds
# configuration, key hashes, and historical analytics only.
#
# tenant_id: multi-tenant ready. The current deployment is single-tenant (the
# operator's account); every channel stores the owner's user id as tenant_id
# and queries scope by it, so one tenant can never touch another's channels.
# viewer-side session rows are keyed by opaque client session ids (no PII —
# never store IP addresses here; the privacy model forbids it).
LIVE_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS live_channels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    slug VARCHAR(64) NOT NULL,
    description VARCHAR(500),
    stream_key_hash TEXT NOT NULL,
    stream_key_prefix VARCHAR(24),
    status VARCHAR(16) NOT NULL DEFAULT 'OFFLINE',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_seen_at TIMESTAMP WITH TIME ZONE,
    UNIQUE(tenant_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_live_channels_tenant ON live_channels(tenant_id);

CREATE TABLE IF NOT EXISTS live_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id UUID NOT NULL REFERENCES live_channels(id) ON DELETE CASCADE,
    session_id VARCHAR(128) NOT NULL,
    peer_id VARCHAR(128),
    started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    ended_at TIMESTAMP WITH TIME ZONE,
    last_heartbeat TIMESTAMP WITH TIME ZONE,
    user_agent VARCHAR(300),
    region VARCHAR(64),
    UNIQUE(channel_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_live_sessions_channel ON live_sessions(channel_id, started_at DESC);

CREATE TABLE IF NOT EXISTS live_analytics_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id UUID NOT NULL REFERENCES live_channels(id) ON DELETE CASCADE,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    duration_seconds FLOAT NOT NULL DEFAULT 0,
    peak_viewers INTEGER NOT NULL DEFAULT 0,
    peak_peers INTEGER NOT NULL DEFAULT 0,
    bytes_p2p BIGINT NOT NULL DEFAULT 0,
    bytes_origin BIGINT NOT NULL DEFAULT 0,
    segments_served INTEGER NOT NULL DEFAULT 0,
    segments_failed INTEGER NOT NULL DEFAULT 0,
    buffering_events BIGINT NOT NULL DEFAULT 0,
    playback_errors BIGINT NOT NULL DEFAULT 0,
    avg_startup_ms INTEGER NOT NULL DEFAULT 0,
    origin_offline_seconds FLOAT NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_live_analytics_channel ON live_analytics_events(channel_id, created_at DESC);
"""

# Connection pools
pg_pool: asyncpg.Pool | None = None
redis_client: Redis | None = None
# Last Redis connection failure reason (str(e), truncated) — surfaced on the
# admin health dashboard so an operator can see WHY the optional Redis
# dependency is down (DNS, TLS, auth, timeout, paused instance) instead of a
# generic "unreachable". Cleared on a successful connection.
last_redis_error: str | None = None


class _TimedPool:
    """Thin proxy over an asyncpg.Pool that injects ``db_pool_acquire_timeout``
    into every ``acquire()`` call.

    Why a proxy instead of monkey-patching: asyncpg's ``Pool`` is a Cython
    class with ``__slots__``, so ``pg_pool.acquire = wrapper`` raises
    ``AttributeError: 'Pool' object attribute 'acquire' is read-only`` (seen on
    asyncpg 0.31.0). That broke the previous in-place wrapper silently — the
    exception was swallowed by run_startup_migrations' try/except, and the pool
    then ran with asyncpg's DEFAULT acquire(), which blocks INDEFINITELY when
    every connection is busy (the failure mode seen at ~300 concurrent users:
    requests hang until the client or gunicorn gives up, surfacing as bare 500s
    instead of the designed retryable 503).

    This class delegates every attribute to the real pool, overriding only
    ``acquire()`` to default the timeout — so all 68 ``pool.acquire()`` call
    sites get the bound wait without being touched, and explicit timeouts
    still win. ``acquire()`` is intentionally NOT async: asyncpg's own
    ``Pool.acquire()`` returns the async context-manager directly (call sites
    do ``async with pool.acquire() as conn:``), so this must mirror that.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: asyncpg.Pool):
        object.__setattr__(self, "_pool", pool)

    def acquire(self, *args, **kwargs):
        kwargs.setdefault("timeout", settings.db_pool_acquire_timeout)
        return self._pool.acquire(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._pool, name)


async def get_db_pool() -> asyncpg.Pool:
    global pg_pool
    if pg_pool is None:
        raw_pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            # Required for Supabase/PgBouncer which doesn't support prepared statements
            statement_cache_size=0,
        )
        pg_pool = _TimedPool(raw_pool)
    return pg_pool


async def get_redis() -> Redis | None:
    """Get Redis client. Returns None if Redis is unavailable (optional dependency).

    Fail-open contract: every caller treats a None return as "use the in-memory
    fallback", so a Redis outage must degrade, never 500. The cached client is
    therefore liveness-checked on EVERY call: a client whose connection died
    (managed-Redis recycle/pause, network blip) is torn down and the caller
    falls back, instead of being handed a dead client whose first command
    raises mid-request. That crash previously propagated through the global
    RateLimitMiddleware's unguarded redis.get/incr/expire into Starlette's
    plain-text 500, which the frontend surfaces as the generic "Login failed.
    Please try again." (no JSON detail).
    """
    global redis_client, last_redis_error
    if redis_client is None:
        try:
            redis_client = await Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                # Bound every connection attempt and command. redis-py's
                # defaults are NO timeouts: a connection that accepts but
                # never responds (half-open after a managed-Redis recycle) or
                # a wedged socket would block a request INDEFINITELY — the
                # failure mode that ended in gunicorn killing requests and
                # bare 500s. With these, the get_redis liveness check and the
                # callers' in-memory fallbacks see a fast error instead.
                socket_connect_timeout=5,
                socket_timeout=5,
                # Proactively detect dead idle connections: redis-py PINGs a
                # connection that has been idle longer than this before
                # reusing it, so a recycled instance is caught on the NEXT
                # command rather than raising mid-request.
                health_check_interval=30,
                # Reconnect with short jittered backoff on CONNECTION-level
                # errors only (a dead pooled connection is replaced with a
                # fresh one). TimeoutError is deliberately NOT retried — a
                # genuinely slow/stuck Redis must fail fast to the in-memory
                # fallback, not loop re-issuing commands.
                retry=Retry(ExponentialWithJitterBackoff(cap=1.0, base=0.1), 3),
                retry_on_error=[ConnectionError],
            )
            await redis_client.ping()
            last_redis_error = None
        except Exception as e:
            logger.warning("Redis unavailable — skipping (optional dependency): %s", e)
            last_redis_error = str(e)[:300]
            redis_client = None
            return None
        return redis_client
    # Cached client — verify it still serves commands before handing it out
    # (a stale client from before a Redis restart/pause would otherwise raise
    # on the caller's first command).
    try:
        await redis_client.ping()
        return redis_client
    except Exception as e:
        logger.warning(
            "Redis connection lost — resetting client (in-memory fallback): %s", e
        )
        last_redis_error = str(e)[:300]
        try:
            await redis_client.aclose()
        except Exception:
            pass
        redis_client = None
        return None


async def close_db():
    global pg_pool
    if pg_pool:
        await pg_pool.close()
        pg_pool = None


async def close_redis():
    global redis_client, last_redis_error
    if redis_client:
        await redis_client.close()
    redis_client = None
    last_redis_error = None
