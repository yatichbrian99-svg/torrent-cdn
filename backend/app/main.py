import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

logger = logging.getLogger(__name__)

import os
import threading

from .ai import routes as ai_routes
from .ai.config import get_ai_settings
from .ai.errors import AIError
from .ai.routes import ai_error_handler
from .ai.store import AI_TABLES_DDL
from .config import check_clock_skew
from .config import get_settings as get_host_settings
from .database import (
    API_KEYS_TABLE_DDL,
    CORE_TABLES_DDL,
    LIVE_TABLES_DDL,
    NOTIFICATIONS_TABLE_DDL,
    PAYOUTS_TABLE_DDL,
    USERS_TABLE_DDL,
    close_db,
    close_redis,
    get_db_pool,
)
from .routes import (
    admin,
    analytics,
    auth,
    billing,
    csp,
    files,
    live,
    live_internal,
    notifications,
    payouts,
    upload,
    verification,
)
from .security import enforce_max_size, get_client_ip

settings = get_host_settings()

# TCDN-AI: feature flag lives OUTSIDE the TCDN_AI_ prefix so the CDN can import
# the backend without any AI configuration present. Default is OFF. When off the
# app does not mount /api/ai/v1, does not run AI_TABLES_DDL, and does not start
# the AI background sweep.
_ai_settings = None
try:
    _ai_settings = get_ai_settings()
except Exception as e:
    logger.debug("TCDN-AI settings unavailable: %s", e)
_ai_enabled = bool(_ai_settings) and _ai_settings.is_ai_enabled_for_host(
    settings.app_env
)


async def run_startup_migrations():
    """Run schema migrations at startup. Idempotent — safe to run multiple times."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Create the users table FIRST (full schema) so a fresh database has
            # all columns before the ALTERs run and so NOTIFICATIONS_TABLE_DDL
            # (which REFERENCES users) can be created. Idempotent on existing DBs.
            await conn.execute(USERS_TABLE_DDL)
            # Core CDN tables must exist before the ALTER batch below touches
            # them; otherwise the batch aborts on a fresh database and takes
            # the AI schema down with it.
            await conn.execute(CORE_TABLES_DDL)
            await conn.execute(NOTIFICATIONS_TABLE_DDL)
            # API keys reference users — create after both (idempotent on existing DBs).
            await conn.execute(API_KEYS_TABLE_DDL)
            # Payout tables reference users — after users (idempotent on existing DBs).
            await conn.execute(PAYOUTS_TABLE_DDL)
            # Live tables reference users — after users (idempotent on existing DBs).
            await conn.execute(LIVE_TABLES_DDL)
            await conn.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255);
                ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMP WITH TIME ZONE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(20) UNIQUE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by UUID REFERENCES users(id);
                ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_bonus_gb FLOAT DEFAULT 0;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_backup_codes TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS security_stamp TIMESTAMP WITH TIME ZONE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_used TEXT;
                ALTER TABLE uploads ADD COLUMN IF NOT EXISTS thumbnail_url TEXT;
                ALTER TABLE uploads ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
                ALTER TABLE uploads ADD COLUMN IF NOT EXISTS content_hash TEXT;
                -- DFS M1: logical folders. NULL = root (every existing upload
                -- stays at root — no data migration). Owner-scoped uniqueness
                -- (user_id, folder_path, filename) prevents two files with the
                -- same name in the same folder for the same owner; existing
                -- duplicate rows are a pre-existing-data risk resolved lazily
                -- (rename endpoints return 409 instead of relying on the index
                -- to reject them, so legacy duplicates keep working).
                ALTER TABLE uploads ADD COLUMN IF NOT EXISTS folder_path VARCHAR(512);
                CREATE INDEX IF NOT EXISTS idx_uploads_user_folder ON uploads(user_id, folder_path);
                -- DFS M1 share links: capability URLs with expiry / password /
                -- download cap / revocation. Only the SHA-256 of the random
                -- token is stored (same pattern as api_keys.stream_key); the
                -- plaintext token appears exactly once, in the create response.
                CREATE TABLE IF NOT EXISTS share_links (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    file_id UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
                    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    password_hash TEXT,
                    max_downloads INTEGER,
                    download_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TIMESTAMP WITH TIME ZONE,
                    revoked_at TIMESTAMP WITH TIME ZONE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_share_links_file ON share_links(file_id);
                CREATE INDEX IF NOT EXISTS idx_share_links_owner ON share_links(owner_id);
                -- Live fix: the initial CREATE TABLE IF NOT EXISTS ran in production
                -- WITHOUT this column (tests mock the DB, so the mismatch slipped
                -- through); every live_channels query selects/inserts it → 500s.
                ALTER TABLE live_channels ADD COLUMN IF NOT EXISTS stream_key_prefix VARCHAR(24);
                -- Live fix: slugs were UNIQUE(tenant_id, slug) but slug-keyed
                -- lookups (public playback, MediaMTX auth hook, viewer sessions)
                -- are global — two tenants could shadow the same slug. Enforce a
                -- global unique slug; conflicting rows on upgrade are a deploy
                -- blocker to resolve manually (they cannot coexist safely).
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_channels_slug ON live_channels(slug);
                CREATE INDEX IF NOT EXISTS idx_uploads_content_hash ON uploads(content_hash);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_user_idempotency ON uploads(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at DESC);
                -- Viewer hot path: every aggregate query filters analytics by
                -- file_id and SUMs bytes over a recent time window. Without the
                -- file_id index each poll degrades to a full-table scan as the
                -- analytics table grows (200k viewers × events). The created_at
                -- column lets Postgres prune old partitions of the index for
                -- the 7-day bandwidth-history query too.
                CREATE INDEX IF NOT EXISTS idx_analytics_file_created ON analytics(file_id, created_at DESC);
            """)
    except Exception as e:
        logger.warning("Startup migrations skipped (first run?): %s", e)

    # TCDN-AI: additive, idempotent schema for the coordinator. Kept in its own
    # try block so a failure in the CDN migrations can never leave the AI
    # feature enabled but schema-less — and gated on the flag, because the
    # flag-off promise is that this deployment does not touch the AI schema at
    # all. It used to run unconditionally (and log the failure at DEBUG when the
    # flag was off), which created 17 ai_* tables in a database that was
    # supposedly not running TCDN-AI.
    if _ai_enabled:
        try:
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                await conn.execute(AI_TABLES_DDL)
                logger.info("TCDN-AI startup migrations completed")
        except Exception as e:
            logger.error("TCDN-AI schema migration failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: run migrations before accepting requests
    await run_startup_migrations()

    # Startup: clock-skew guard (production only, non-blocking, fail-open).
    # JWT exp (email verify 24h, password reset 1h) is minted/validated against
    # the local clock; a badly drifted instance silently breaks token lifetimes.
    # Runs in a daemon thread so an unreachable NTP reference can never delay
    # readiness, and so tests (which run in development mode) never hit the network.
    if settings.app_env == "production":
        threading.Thread(target=check_clock_skew, daemon=True).start()

    # Pre-warm the public-file metadata cache so the first viewers after a
    # deploy don't all miss Redis simultaneously (each miss = 1 DB query;
    # 10K concurrent viewers = 10K DB queries in the cold-cache window).
    # Best-effort, non-blocking — runs in a background task so startup
    # isn't delayed by the DB round-trip.
    async def _warm_cache():
        try:
            from .routes.files import warm_public_file_cache

            await warm_public_file_cache()
        except Exception as e:
            logger.warning("Cache warm failed: %s", e)

    asyncio.create_task(_warm_cache())

    # Production-only background task: periodically delete storage objects that
    # were PUT but never finalized (presign session expired / DB insert failed).
    # Never started in development so the tests (TestClient lifespan) stay
    # hermetic and never list/delete from storage.
    orphan_sweep_task = None
    if settings.app_env == "production":
        orphan_sweep_task = asyncio.create_task(upload.run_orphan_sweep_loop())
        logger.info(
            "Orphan storage sweep started (every %ss)",
            upload.ORPHAN_SWEEP_INTERVAL_SECONDS,
        )
    # Analytics ingestion drainer: batch-INSERTs buffered viewer events every
    # ~2s (one INSERT per ~500 events instead of one per request). Keeps the
    # DB write rate flat no matter how many concurrent viewers post events.
    from .routes.analytics import ensure_analytics_flusher

    ensure_analytics_flusher()
    # TCDN-AI: expire jobs whose worker died before reporting, and release the
    # customer's hold. Without this, a crashed worker leaves credits reserved
    # forever. Runs only when the feature flag is on. The host app keeps this
    # off in development and in tests (the database may not be reachable there,
    # and a background task that opens a real pool during a TestClient startup
    # hangs the suite). The receipt path still releases the hold when a late
    # receipt arrives.
    ai_expiry_task = None
    if _ai_enabled:
        ai_expiry_task = asyncio.create_task(ai_routes.expire_jobs_loop())

    # Live state monitor: drives OFFLINE→STARTING→LIVE transitions from the
    # actual MediaMTX/stream activity, expires stale peer/segment state, and
    # rolls Redis live counters into durable live_analytics_events rows.
    # Every tick isolates failures per channel — one broken channel must never
    # crash the monitor (and never affect another channel's state).
    from .live_state import start_live_monitor

    start_live_monitor()
    yield
    # Shutdown
    if orphan_sweep_task is not None:
        orphan_sweep_task.cancel()
        try:
            await orphan_sweep_task
        except asyncio.CancelledError:
            pass
    if ai_expiry_task is not None:
        ai_expiry_task.cancel()
        try:
            await ai_expiry_task
        except asyncio.CancelledError:
            pass
    await close_db()
    await close_redis()


# API docs (Swagger UI / ReDoc / OpenAPI schema) are development-only.
# Exposing the full schema in production gives attackers a precise route map
# before their first request (OWASP API3:2019 — Excessive Data Exposure).
# Enabled outside production by default, or explicitly via ENABLE_API_DOCS=true.
docs_enabled = settings.app_env != "production" or settings.api_docs_enabled

app = FastAPI(
    title="Torrent CDN API",
    description="P2P CDN platform powered by WebTorrent",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
)

# CORS
# NOTE: torrentcdn.com / api.torrentcdn.com were REMOVED — the domain is no
# longer owned, so a stale allow-list entry would grant credential-bearing
# cross-origin reads to whoever re-registers it (CORS + allow_credentials).
# With allow_credentials=True the allow-list must only ever contain origins we
# control; any env-supplied origins (CORS_ORIGINS) get the same scrutiny.
# The apex (the bare torrentcdn.store host, no www) is intentionally NOT
# allow-listed: it 308-redirects to www, so no page is ever served from the
# apex origin and its Origin header never legitimately occurs
# (backend/scripts/check_domains.sh enforces this).
cors_origins = [
    "http://frontend:3000",
    "https://torrent-cdn.vercel.app",
    "https://www.torrentcdn.store",
]
# Allow localhost only in development — production never serves pages from
# localhost, so an allow-list entry would grant credential-bearing cross-origin
# reads to anyone who discovers the direct backend URL.
if settings.app_env != "production":
    cors_origins.append("http://localhost:3000")

extra = os.getenv("CORS_ORIGINS", "")
if extra:
    cors_origins.extend([o.strip() for o in extra.split(",")])

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Range", "Origin", "Accept"],
)

# Trusted host middleware — restrict in production, allow all in development
if settings.app_env == "production":
    # Build trusted hosts from env var + auto-detect common platforms
    trusted_hosts = [h.strip() for h in settings.trusted_hosts.split(",") if h.strip()]
    # Auto-add the hostname from APP_URL (frontend domain, e.g. torrent-cdn.vercel.app)
    parsed = urlparse(settings.app_url)
    if parsed.hostname:
        trusted_hosts.append(parsed.hostname)
        if not parsed.hostname.startswith("."):
            trusted_hosts.append(f".{parsed.hostname}")
    # Always allow Render's onrender.com domain (where this backend runs)
    trusted_hosts.extend(["onrender.com", "*.onrender.com"])

    # Allow localhost and testserver for health checks and CI/TestClient
    trusted_hosts.extend(["localhost", "127.0.0.1", "testserver"])
else:
    trusted_hosts = ["*"]

app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)


# Content Security Policy middleware
class CSPMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._is_production = settings.app_env == "production"
        self._csp = self._build_csp(self._is_production)
        self._csp_report_only = settings.csp_report_only
        self._report_uri = settings.csp_report_url
        self._reporting_endpoint = f'csp-endpoint="{self._report_uri}"'

    def _build_csp(self, is_production: bool) -> str:
        # Script sources — same-origin only. WebTorrent is self-hosted and pinned
        # at /vendor/webtorrent.min.js (with SRI), so no third-party script CDNs
        # or inline scripts are allowed.
        script_src = "'self'"

        # Brave/Chrome compatible fallback: strict-dynamic disables 'unsafe-inline'
        # when supported, so we keep 'unsafe-inline' for older browsers
        # that don't support strict-dynamic

        # Derive media/connect sources from the configured app_url
        if is_production:
            app_origin = settings.app_url.rstrip("/")
            minio_src = app_origin
        else:
            minio_src = "http://localhost:9000"

        return (
            "default-src 'self'; "
            f"script-src {script_src}; "
            "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
            "font-src 'self' fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            f"media-src 'self' blob: {minio_src}; "
            "connect-src 'self' "
            "wss://tracker.webtorrent.dev "
            "wss://tracker.openwebtorrent.com "
            "wss://tracker.files.fm:7073 "
            f"{minio_src}; "
            "frame-src 'self'; "
            "worker-src 'self' blob:; "
            "base-uri 'self'"
        )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Skip report endpoint itself to avoid loops (handle trailing slash)
        if request.url.path.rstrip("/") == self._report_uri.rstrip("/"):
            return response

        # Base CSP directives (built once at startup) + reporting directives appended per-request
        csp = self._csp + f"; report-uri {self._report_uri}; report-to csp-endpoint"

        header_name = (
            "Content-Security-Policy-Report-Only"
            if self._csp_report_only
            else "Content-Security-Policy"
        )
        response.headers[header_name] = csp
        response.headers["Reporting-Endpoints"] = self._reporting_endpoint

        # Additional security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Every API response is dynamic JSON consumed by the SPA — never let a
        # shared intermediary (Vercel edge, CDN, corporate proxy) cache it.
        # Sensitive payloads (/auth/me, /files, /billing, /admin/*) must never
        # be served stale to a different user from a cache.
        # EXCEPTION: routes that explicitly set a shared-cache policy (the
        # public viewer hot path — /files/public/*, /analytics/public/*) set
        # their own Cache-Control BEFORE this middleware runs; only default
        # responses that didn't opt in get the no-store blanket. Without this
        # guard the blanket clobbered the short-TTL CDN caching that keeps
        # 200k-viewer metadata/stats floods away from the origin.
        if "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        # API responses are JSON consumed via CORS-enabled fetch — CORP is safe
        # here and blocks no-cors cross-origin embedding of API payloads.
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), "
            "ambient-light-sensor=(), "
            "autoplay=(), "
            "camera=(), "
            "display-capture=(), "
            "document-domain=(), "
            "encrypted-media=(), "
            "fullscreen=(), "
            "gamepad=(), "
            "geolocation=(), "
            "gyroscope=(), "
            "interest-cohort=(), "
            "magnetometer=(), "
            "microphone=(), "
            "midi=(), "
            "payment=(), "
            "picture-in-picture=(), "
            "publickey-credentials-get=(), "
            "screen-wake-lock=(), "
            "speaker-selection=(), "
            "usb=(), "
            "web-share=()"
        )

        # HSTS — only in production
        if self._is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        return response


# Rate limiting middleware (120 requests per 10 seconds per IP)
class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Global rate limiting — 300 requests per 10 seconds per IP (30 req/s
    sustained). Uses Redis when available, falls back to in-memory.
    Excludes /health, /auth/*, and the already-route-limited public
    endpoints from rate limiting (each carries its own dedicated control).
    """

    def __init__(self, app):
        super().__init__(app)
        self._window = settings.rate_limit_window_seconds  # 10s
        self._limit = settings.rate_limit_global  # 300 requests
        # In-memory fallback: {ip: [(timestamp, count), ...]}
        self._memory_store: dict[str, list[float]] = {}
        # Bounded-store hygiene: the client IP feeds this dict (via X-Forwarded-
        # For, which a direct-to-backend attacker can rotate freely), so a key
        # whose whole window has expired must eventually be dropped — otherwise
        # an attacker could grow memory without bound. Pruning is opportunistic
        # (only once the store passes this threshold) so the hot path stays O(1)
        # while a degraded/Redis-down state can't balloon.
        self._prune_threshold = 512

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for static/no-state endpoints: health check, CSP
        # report collector, API docs (404 in production), and security.txt.
        # None of them touch the DB or Redis, so there is nothing to protect.
        path = request.url.path.rstrip("/")
        if path in {
            "/health",
            "/csp-report",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/.well-known/security.txt",
        }:
            return await call_next(request)

        # Chunked-upload chunk PUTs are exempt from the GLOBAL per-IP limiter:
        # a real file legitimately ships dozens-to-hundreds of ≤4MB chunks in a
        # burst (a 100MB video = 25+ requests), which would blow the budget and
        # 429 mid-upload. These endpoints are already protected at
        # the route layer — JWT auth, session-ownership + index-bounds checks,
        # per-chunk size cap — and the per-IP upload limiter (10/hour) + usage
        # gate still apply at finalize. Session CREATION stays under the global
        # limiter, so an attacker can't mint unbounded sessions either.
        #
        # The public, already-route-limited endpoints are exempt for the same
        # reason: /analytics/event (600/min route limiter), /analytics/public/*
        # and /files/public/* (120/min route limiters) are legitimately fired
        # at high frequency — the embed player posts chunk_downloaded up to
        # 1/sec per viewer, and the demo page polls stats every 5s — and each
        # already carries its own dedicated per-IP limiter in the route. If
        # they also counted against the global burst budget, a single active
        # viewer (or a handful of NAT'd viewers behind one office/school/mobile
        # IP) would 429 unrelated requests for everyone on that IP.
        #
        # /auth/* is exempt for the same reason — every auth endpoint already
        # carries a DEDICATED, tighter per-IP control than this burst cap:
        # login (5 failed attempts / 15 min per IP+email), register (10/hr per
        # IP), forgot-password (1/60s per IP), MFA confirm (5/15 min per IP),
        # and the rest (me, api-key, referral, reset-password, verify, setup,
        # logout) are gated by a non-brute-forceable token or a session. The
        # global budget was double-counting these and 429'd LEGITIMATE logins
        # on busy shared/NAT IPs with a "Try again in 10s" that never cleared
        # while other traffic on the same key kept the bucket full — a user
        # who cannot log in at all is a worse outcome than any burst these
        # dedicated limiters don't already cover.
        if (
            path == "/auth"
            or path.startswith(
                (
                    "/auth/",
                    "/upload/chunk/",
                    "/analytics/public/",
                    "/files/public/",
                    # Live viewer hot path: the player fires a session join + a
                    # heartbeat + an availability report every ~5s per viewer, all
                    # gated by the route's own short-TTL token limiter. The global
                    # budget would double-count them exactly like /auth/*.
                    "/api/live/session",
                    "/api/live/heartbeat",
                    "/api/live/segment/availability",
                    # Playback-info + public status are the OTHER two calls every
                    # viewer fires (page load + keepalive polls), gated by the same
                    # route-level token limiter. Missing from this list they 429'd
                    # the whole 1,000-viewer load test at the global 120/10s budget
                    # (all viewers behind one IP in the test — but the same shape
                    # as a NAT'd office/school IP in production).
                    "/api/live/play/",
                    "/api/live/segment/availability",
                    # MediaMTX → backend auth hook: publishes from NAT'd or
                    # shared-egress publishers (bore.pub relays, cloud relays) all
                    # arrive at this backend from the relay's IP — the global
                    # per-IP budget would 429 the hook for UNRELATED publishers
                    # (observed live: every OBS retry through bore.pub 429'd for
                    # 10s windows). The route self-gates instead: a valid shared
                    # secret passes; wrong/missing secrets get a tight per-IP
                    # limiter inside the route (see live_internal._check_hook_secret).
                    "/api/internal/mediamtx/",
                )
            )
            or path == "/analytics/event"
        ):
            return await call_next(request)

        import time

        client_ip = get_client_ip(request)
        now = time.time()

        # Try Redis first
        redis = None
        try:
            from .database import get_redis

            redis = await get_redis()
        except Exception:
            pass

        if redis is not None:
            try:
                rate_key = f"ratelimit:{client_ip}:global"
                count = await redis.get(rate_key)
                ttl = await redis.ttl(rate_key)
                # A counter with no expiry is a LATCHED bucket: the window it
                # was opened in can never end, so once it reaches the limit that
                # address is refused for ever ("Try again in 10s" that never
                # clears). It happens when the INCR lands but the EXPIRE does
                # not — a Redis blip, a connection reset between the two, or a
                # restored snapshot. The capacity run found it: the loopback
                # key sat at exactly 300 with TTL -1 and every heartbeat and
                # job from that address was 429'd until the key was deleted by
                # hand. Treat an expiry-less key as expired: drop it and make
                # this request the first of a new window.
                latched = ttl is None or ttl < 0
                if count is not None and int(count) >= self._limit and not latched:
                    retry_after = max(1, ttl)
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": f"Rate limit exceeded. Try again in {retry_after}s."
                        },
                        headers={"Retry-After": str(retry_after)},
                    )
                if latched:
                    await redis.delete(rate_key)
                    count = None
                await redis.incr(rate_key)
                # Refresh the expiry on every latched/missing key, not only
                # when GET returned None: the window must always have one.
                if count is None:
                    await redis.expire(rate_key, self._window)
            except Exception:
                # Redis failed mid-request (get_redis()'s liveness ping can't
                # cover the window between the check and these commands). Degrade
                # to the in-memory limiter below — Redis is optional by design,
                # and a dead Redis must throttle, never 500 the request (a 500
                # here previously crashed EVERY non-exempt request — including
                # /auth/login — into a plain-text error the frontend showed as
                # "Login failed. Please try again.").
                logger.warning(
                    "Global rate-limit Redis error — using in-memory fallback for %s",
                    client_ip,
                )
                redis = None
        if redis is None:
            # In-memory fallback — sliding window
            timestamps = self._memory_store.get(client_ip, [])
            cutoff = now - self._window
            # Remove expired timestamps
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self._limit:
                retry_after = int(timestamps[0] + self._window - now) + 1
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": f"Rate limit exceeded. Try again in {retry_after}s."
                    },
                    headers={"Retry-After": str(retry_after)},
                )
            timestamps.append(now)
            self._memory_store[client_ip] = timestamps

            # Opportunistic prune: once the store outgrows the threshold, drop
            # every key whose newest entry has fully expired (same pattern as
            # the per-route fallback limiters). Keeps memory O(live IPs) even
            # under rotated-XFF abuse while staying off the hot path.
            if len(self._memory_store) > self._prune_threshold:
                expired = [
                    k
                    for k, v in self._memory_store.items()
                    if not v or now - v[-1] > self._window
                ]
                for k in expired:
                    del self._memory_store[k]
                enforce_max_size(self._memory_store)

        return await call_next(request)


# Request size limit middleware
class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Check Content-Length header (will only be present for non-chunked requests)
        content_length = request.headers.get("content-length")
        max_bytes = 2 * 1024 * 1024 * 1024  # 2GB
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    return JSONResponse(
                        {"detail": "Request too large"}, status_code=413
                    )
            except (ValueError, TypeError):
                pass  # Invalid content-length header — let other checks handle it

        # For chunked transfer encoding, we can't know size upfront,
        # but the upload route has its own file size check via seek(0, 2)
        # which reads the full file before processing
        return await call_next(request)


# CSPMiddleware must be OUTERMOST (added last in Starlette's prepend-order)
# so 429 rate-limit and 413 body-size rejections — which short-circuit BELOW
# it — still carry the full security-header set (CSP, no-store, X-XSS, …).
app.add_middleware(RateLimitMiddleware)
app.add_middleware(MaxBodySizeMiddleware)
app.add_middleware(CSPMiddleware)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """L2 FIX: Return a generic error for JSON parse / validation failures.

    FastAPI's default handler returns the Pydantic schema (field names,
    types, input values), which reveals the framework (FastAPI/Pydantic)
    and the expected request body shape. A generic message prevents this
    reconnaissance while still giving the caller enough to retry correctly.
    """
    # Distinguish JSON decode errors (malformed body) from schema errors
    # (missing/wrong fields) — the caller needs to know which one.
    errors = exc.errors()
    is_json_error = any(e.get("type") == "json_invalid" for e in errors)
    if is_json_error:
        detail = "Invalid JSON in request body"
    else:
        detail = "Invalid request"
    return JSONResponse(
        status_code=422,
        content={"detail": detail},
    )


@app.exception_handler(asyncio.TimeoutError)
async def _timeout_handler(request: Request, exc):
    # Graceful degradation under saturation. The only asyncio.TimeoutError that
    # can reach this handler is the bounded DB-pool acquire wait in
    # database.get_db_pool (every other timeout in the app is an httpx/
    # Paystack/import timeout — separate exception types — or is caught where
    # it is raised). When the pool is exhausted past db_pool_acquire_timeout
    # seconds, return a retryable 503 (the frontend treats 502/503/504 as
    # transient and retries) instead of hanging the request until the client
    # or gunicorn's --timeout 120 gives up.
    return JSONResponse(
        status_code=503,
        content={"detail": "Service is busy — please retry in a moment."},
        headers={"Retry-After": "5"},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    # Any unhandled exception — route, dependency, OR middleware (FastAPI
    # registers this on the OUTERMOST ServerErrorMiddleware, so it also catches
    # BaseHTTPMiddleware failures) — returns a structured JSON error instead of
    # Starlette's default plain-text "Internal Server Error". The frontend's
    # apiErrorMessage/showError read data.detail, so a bare 500 previously fell
    # through to the generic "Login failed. Please try again." fallback even
    # though the real problem was server-side; a JSON detail surfaces an actual
    # message (and the api layer's retry logic can branch on it).
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )


# Routes
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(files.router)
app.include_router(analytics.router)
app.include_router(billing.router)
app.include_router(payouts.router)
app.include_router(csp.router)
app.include_router(admin.router)
app.include_router(verification.router)
app.include_router(notifications.router)
app.include_router(live.router)
app.include_router(live_internal.router)
if _ai_enabled:
    # TCDN-AI coordinator API + its separate admin surface. Mounted only when the
    # feature flag is on, so a deployment without AI config can still import and
    # run the CDN backend.
    app.include_router(ai_routes.router)
    app.include_router(ai_routes.admin_router)
    # OpenAI-compatible surface (gateway mode), mounted at the app root so an
    # unmodified OpenAI client can use base_url=".../v1". Same flag, same auth.
    from .ai import routes_openai as ai_openai_routes

    app.include_router(ai_openai_routes.router)
    app.add_exception_handler(AIError, ai_error_handler)


SECURITY_TXT = """Contact: https://www.torrentcdn.store
Contact: https://github.com/yatichbrian99-svg/torrent-cdn/security
Expires: 2027-08-02T00:00:00.000Z
Preferred-Languages: en
Acknowledgments: https://github.com/yatichbrian99-svg/torrent-cdn
"""


@app.get("/health")
async def health_check():
    # No service name here — keep the endpoint free of implementation fingerprinting.
    return {"status": "healthy"}


@app.get("/api/health/live")
async def live_health():
    """Live pipeline health — the coarse status a monitoring probe needs.

    Deliberately UNAUTHENTICATED but detail-free (same posture as /health):
    exposes only the overall state machine booleans, never MediaMTX URLs,
    keys, or internal topology. Component-level diagnostics live on the
    admin health dashboard (which already reports Redis/DB checks).

    Distinguishes APPLICATION health (monitor loop, Redis) from MEDIA-PIPELINE
    health (MediaMTX reachable + segment discovery flowing) — an operator must
    be able to tell "backend broken" from "ingest broken" at a glance.
    """
    from .database import get_redis
    from .live_state import get_live_monitor_health

    h = get_live_monitor_health()
    redis_ok = False
    try:
        redis_ok = (await get_redis()) is not None
    except Exception:
        redis_ok = False
    mediamtx_ok = bool(h.get("mediamtx_reachable"))
    media_pipeline_ok = mediamtx_ok or bool(h.get("active_channels"))
    app_ok = bool(h.get("monitor_running"))
    return {
        "status": "healthy"
        if (app_ok and (media_pipeline_ok or not h.get("mediamtx_configured")))
        else "degraded",
        "application": {
            "monitor_running": app_ok,
            "redis": redis_ok,
        },
        "media_pipeline": {
            "mediamtx_configured": bool(h.get("mediamtx_configured")),
            "mediamtx_reachable": mediamtx_ok,
            "segment_discovery_active": media_pipeline_ok,
        },
        "active_channels": int(h.get("active_channels") or 0),
        "last_error": h.get("last_error"),
    }


@app.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt():
    """RFC 9116 security contact for researchers (also served on the frontend)."""
    return Response(content=SECURITY_TXT, media_type="text/plain; charset=utf-8")
