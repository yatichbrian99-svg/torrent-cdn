from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import get_settings
from .database import init_db, close_db, close_redis
from .routes import auth, upload, files, analytics, billing

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://frontend:3000",
        settings.app_url,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trusted host middleware
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])


# Content Security Policy middleware
class CSPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' unpkg.com cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
            "font-src 'self' fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob: http://localhost:9000 https://*.torrentcdn.com; "
            "connect-src 'self' "
            "wss://tracker.openwebtorrent.com "
            "wss://tracker.btorrent.xyz "
            "wss://tracker.fastcast.nz "
            "http://localhost:9000 "
            "https://*.torrentcdn.com; "
            "frame-src 'self'; "
            "worker-src 'self' blob:; "
            "base-uri 'self'"
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


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "torrent-cdn-api"}
