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
            """)
            # Promote the first registered user to admin if none exists
            await conn.execute("""
                UPDATE users SET is_admin = TRUE
                WHERE id = (SELECT id FROM users ORDER BY created_at ASC LIMIT 1)
                AND NOT EXISTS (SELECT 1 FROM users WHERE is_admin = TRUE)
            """)
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
    allow_methods=["*"],
    allow_headers=["*"],
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
        # Script sources — only unpkg is used by sdk.js
        script_src = "'self' 'unsafe-inline' unpkg.com"

        # Derive media/connect sources from the configured app_url
        if is_production:
            # In production, allow the app's own domain + protocol for media/connect
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


# Request size limit middleware
class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.headers.get("content-length"):
            if int(request.headers["content-length"]) > 2 * 1024 * 1024 * 1024:  # 2GB
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        return await call_next(request)


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
