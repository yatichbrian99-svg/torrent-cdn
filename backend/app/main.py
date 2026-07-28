from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

# Routes
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(files.router)
app.include_router(analytics.router)
app.include_router(billing.router)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "torrent-cdn-api"}
