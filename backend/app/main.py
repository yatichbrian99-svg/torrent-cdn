import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

logger = logging.getLogger(__name__)

from .config import get_settings
from .database import close_db, close_redis, get_db_pool
import os

from .routes import auth, upload, files, analytics, billing, csp, admin, verification

settings = get_settings()


async def run_startup_migrations():
    """Run schema migrations at startup. Idempotent — safe to run multiple times."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255);
                ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMP WITH TIME ZONE;
            """)
            # Promote the first registered user to admin if none exists
            if not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM users WHERE is_admin = TRUE)"
            ):
                first_id = await conn.fetchval(
                    "SELECT id FROM users ORDER BY created_at ASC LIMIT 1"
                )
                if first_id:
                    await conn.execute(
                        "UPDATE users SET is_admin = TRUE WHERE id = $1", first_id
                    )
                    logger.info("Promoted user %s to admin at startup", first_id)
            logger.info("Startup migrations completed")
    except Exception as e:
        logger.warning("Startup migrations skipped (first run?): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: run migrations before accepting requests
    await run_startup_migrations()
    yield
    # Shutdown
    await close_db()
    await close_redis()


app = FastAPI(
    title="Torrent CDN API",
    description="P2P CDN platform powered by WebTorrent",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
cors_origins = [
    "http://localhost:3000",
    "http://frontend:3000",
    "https://torrent-cdn.vercel.app",
    "https://torrentcdn.store",
    "https://www.torrentcdn.store",
    "https://torrentcdn.com",
    "https://api.torrentcdn.com",
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
        # Script sources — strict-dynamic removes need for 'unsafe-inline'
        # unpkg.com is needed for WebTorrent SDK in embeds
        # strict-dynamic allows scripts loaded by trusted scripts to run
        script_src = "'self' 'unsafe-inline' unpkg.com"

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
            "wss://tracker.openwebtorrent.com "
            "wss://tracker.btorrent.xyz "
            "wss://tracker.fastcast.nz "
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


# Rate limiting middleware (5 requests per 10 seconds per IP)
class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Global rate limiting — 5 requests per 10 seconds per IP.
    Uses Redis when available, falls back to in-memory.
    Excludes /health from rate limiting.
    """
    def __init__(self, app):
        super().__init__(app)
        self._window = settings.rate_limit_window_seconds  # 10s
        self._limit = settings.rate_limit_global  # 5 requests
        # In-memory fallback: {ip: [(timestamp, count), ...]}
        self._memory_store: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health check and CSP report endpoint
        path = request.url.path.rstrip("/")
        if path == "/health" or path == "/csp-report":
            return await call_next(request)

        import time
        client_ip = request.client.host if request.client else "unknown"
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


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "torrent-cdn-api"}
