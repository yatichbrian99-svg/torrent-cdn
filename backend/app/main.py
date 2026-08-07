import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

logger = logging.getLogger(__name__)

from .config import check_clock_skew, get_settings
from .database import close_db, close_redis, get_db_pool
from .security import get_client_ip
import os
import threading

from .routes import auth, upload, files, analytics, billing, csp, admin, verification, notifications
from .database import API_KEYS_TABLE_DDL, NOTIFICATIONS_TABLE_DDL, USERS_TABLE_DDL

settings = get_settings()


async def run_startup_migrations():
    """Run schema migrations at startup. Idempotent — safe to run multiple times."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Create the users table FIRST (full schema) so a fresh database has
            # all columns before the ALTERs run and so NOTIFICATIONS_TABLE_DDL
            # (which REFERENCES users) can be created. Idempotent on existing DBs.
            await conn.execute(USERS_TABLE_DDL)
            await conn.execute(NOTIFICATIONS_TABLE_DDL)
            # API keys reference users — create after both (idempotent on existing DBs).
            await conn.execute(API_KEYS_TABLE_DDL)
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
                ALTER TABLE uploads ADD COLUMN IF NOT EXISTS thumbnail_url TEXT;
                ALTER TABLE uploads ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
                ALTER TABLE uploads ADD COLUMN IF NOT EXISTS content_hash TEXT;
                CREATE INDEX IF NOT EXISTS idx_uploads_content_hash ON uploads(content_hash);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_user_idempotency ON uploads(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at DESC);
            """)
            logger.info("Startup migrations completed")
    except Exception as e:
        logger.warning("Startup migrations skipped (first run?): %s", e)


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
    yield
    # Shutdown
    if orphan_sweep_task is not None:
        orphan_sweep_task.cancel()
        try:
            await orphan_sweep_task
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
    "http://localhost:3000",
    "http://frontend:3000",
    "https://torrent-cdn.vercel.app",
    "https://www.torrentcdn.store",
]

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
        self._reporting_endpoint = f"csp-endpoint=\"{self._report_uri}\""

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

        header_name = "Content-Security-Policy-Report-Only" if self._csp_report_only else "Content-Security-Policy"
        response.headers[header_name] = csp
        response.headers["Reporting-Endpoints"] = self._reporting_endpoint

        # Additional security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Every API response is dynamic JSON consumed by the SPA — never let a
        # shared intermediary (Vercel edge, CDN, corporate proxy) cache it.
        # Sensitive payloads (/auth/me, /files, /billing, /admin/*) must never
        # be served stale to a different user from a cache.
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


app.add_middleware(CSPMiddleware)


# Rate limiting middleware (30 requests per 10 seconds per IP)
class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Global rate limiting — 30 requests per 10 seconds per IP (3 req/s sustained).
    Uses Redis when available, falls back to in-memory.
    Excludes /health from rate limiting.
    """
    def __init__(self, app):
        super().__init__(app)
        self._window = settings.rate_limit_window_seconds  # 10s
        self._limit = settings.rate_limit_global  # 30 requests
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
        if path in {"/health", "/csp-report", "/docs", "/redoc", "/openapi.json", "/.well-known/security.txt"}:
            return await call_next(request)

        # Chunked-upload chunk PUTs are exempt from the GLOBAL per-IP limiter:
        # a real file legitimately ships dozens-to-hundreds of ≤4MB chunks in a
        # burst (a 100MB video = 25+ requests), which would blow the budget and
        # 429 mid-upload. These endpoints are already protected at
        # the route layer — JWT auth, session-ownership + index-bounds checks,
        # per-chunk size cap — and the per-IP upload limiter (10/hour) + usage
        # gate still apply at finalize. Session CREATION stays under the global
        # limiter, so an attacker can't mint unbounded sessions either.
        if path.startswith("/upload/chunk/"):
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
            rate_key = f"ratelimit:{client_ip}:global"
            count = await redis.get(rate_key)
            if count and int(count) >= self._limit:
                # Check if the window has passed
                ttl = await redis.ttl(rate_key)
                retry_after = max(1, ttl) if ttl and ttl > 0 else self._window
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded. Try again in {retry_after}s."},
                    headers={"Retry-After": str(retry_after)},
                )
            await redis.incr(rate_key)
            if count is None:
                await redis.expire(rate_key, self._window)
        else:
            # In-memory fallback — sliding window
            timestamps = self._memory_store.get(client_ip, [])
            cutoff = now - self._window
            # Remove expired timestamps
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self._limit:
                retry_after = int(timestamps[0] + self._window - now) + 1
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded. Try again in {retry_after}s."},
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
                    k for k, v in self._memory_store.items()
                    if not v or now - v[-1] > self._window
                ]
                for k in expired:
                    del self._memory_store[k]

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
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            except (ValueError, TypeError):
                pass  # Invalid content-length header — let other checks handle it

        # For chunked transfer encoding, we can't know size upfront,
        # but the upload route has its own file size check via seek(0, 2)
        # which reads the full file before processing
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)
app.add_middleware(MaxBodySizeMiddleware)

# Routes
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(files.router)
app.include_router(analytics.router)
app.include_router(billing.router)
app.include_router(csp.router)
app.include_router(admin.router)
app.include_router(verification.router)
app.include_router(notifications.router)


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


@app.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt():
    """RFC 9116 security contact for researchers (also served on the frontend)."""
    return Response(content=SECURITY_TXT, media_type="text/plain; charset=utf-8")
