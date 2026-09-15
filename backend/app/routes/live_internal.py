"""Internal live routes — MediaMTX integration.

NEVER routed publicly: mount only on the internal network (docker network /
loopback). The MediaMTX external-auth hook consults the channel table (hashed
stream keys) on every publish/read, and the ingest webhook registers segments
as they appear in the HLS window.

MediaMTX external auth contract (v1.x, authMethod: http) — POST JSON:
    {user, password, token, ip, action, path, protocol, id, query, userAgent}
2xx allows; anything else rejects. Empty user/password is normal (RTSP/RTMP
challenge flow) — we answer 401 so MediaMTX re-asks with credentials.
"""
import logging
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..database import get_db_pool
from ..live_models import hash_stream_key
from ..live_state import (
    active_peers,
    register_segment,
    set_channel_meta,
    clear_channel,
)
from ..security_events import record as record_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal/mediamtx", tags=["live-internal"])

# Ingest webhook shared secret (MEDIAMTX_HOOK_SECRET env — MediaMTX sends it
# in the X-Hook-Secret header / ?secret= query). Optional but recommended when
# the hook is reachable from anything but the MediaMTX container.
_HOOK_SECRET = None


def _hook_secret() -> str:
    global _HOOK_SECRET
    if _HOOK_SECRET is None:
        _HOOK_SECRET = os.environ.get("MEDIAMTX_HOOK_SECRET", "")
    return _HOOK_SECRET


def _check_hook_secret(request: Request) -> None:
    secret = _hook_secret()
    if not secret:
        return  # not configured — rely on network isolation
    presented = request.headers.get("x-hook-secret") or ""
    if not presented:
        presented = (request.query_params.get("secret") or "")
    if presented != secret:
        raise HTTPException(status_code=403, detail="Forbidden")


def _slug_from_path(path: str) -> str | None:
    """Map a MediaMTX path name to a channel slug.

    Publishes arrive as `live/<slug>` (RTMP app/stream), HLS reads as
    `<slug>` (the path name itself)."""
    if not path:
        return None
    path = path.strip("/")
    if path.startswith("live/"):
        path = path[len("live/"):]
    slug = path.split("/")[0]
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        return slug
    return None


async def _load_channel_by_slug(slug: str):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """SELECT id, tenant_id, owner_id, name, slug, description, stream_key_hash,
                      stream_key_prefix, status, enabled, created_at, updated_at, last_seen_at
               FROM live_channels WHERE slug = $1""",
            slug,
        )


@router.post("/auth")
async def mediamtx_auth(request: Request):
    """External authentication for MediaMTX (publish + read).

    Publish: user must be the channel slug and password the stream key —
    verified against the stored SHA-256 hash (never logged, constant-time).
    Read (HLS/WebRTC): allowed for enabled channels; the `query` may carry a
    playback token for deployments that lock reads down, but the default
    posture keeps public HLS readable and gates viewer APIs by token.
    """
    # MediaMTX sends a JSON body; parse defensively.
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = str(body.get("user") or "")
    password = str(body.get("password") or "")
    token = str(body.get("token") or "")
    action = str(body.get("action") or "")
    path = str(body.get("path") or "")
    protocol = str(body.get("protocol") or "")

    slug = _slug_from_path(path)
    if not slug:
        # Unknown path naming → reject (do not allow arbitrary MediaMTX paths).
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    channel = await _load_channel_by_slug(slug)
    if not channel or not channel.get("enabled", True):
        # 404-style rejection: do not reveal whether the channel exists.
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    if action == "publish":
        presented = password or token or user
        presented_hash = hash_stream_key(presented)
        stored = channel.get("stream_key_hash") or ""
        import hmac as _hmac

        if not stored or not _hmac.compare_digest(presented_hash, stored):
            record_event("live_publish_denied", detail=f"slug={slug}")
            return JSONResponse(status_code=403, content={"detail": "forbidden"})
        # Authorized publish → STARTING marker; the first segment flips it to LIVE.
        await set_channel_meta(slug, publish_seen=str(time.time()))
        await _touch_last_seen(channel["id"])
        record_event("live_publish_ok", detail=f"slug={slug}")
        return JSONResponse(status_code=200, content={"detail": "ok"})

    if action == "read":
        # Reads stay open for enabled channels (HLS URL carries no secret);
        # playback privacy is enforced at the API/viewer-token layer.
        return JSONResponse(status_code=200, content={"detail": "ok"})

    if action in {"playback", "api", "metrics", "pprof"}:
        # Never delegated to us by a correctly-configured MediaMTX
        # (authHTTPExclude) — reject defensively if it ever arrives.
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    return JSONResponse(status_code=403, content={"detail": "forbidden"})


async def _touch_last_seen(channel_id) -> None:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE live_channels SET last_seen_at = NOW(), status = 'STARTING' WHERE id = $1",
                channel_id,
            )
    except Exception as e:  # noqa: BLE001 — auth hook must stay fast/crash-free
        logger.warning("last_seen update failed: %s", e)


@router.post("/ingest")
async def mediamtx_ingest(request: Request):
    """Segment-registration webhook (optional hardening).

    If the deployment wires MediaMTX's runOnReady/runOnRead hooks (or an
    HLS-directory watcher) to POST here, segments register the moment they
    are muxed. Without the webhook, registration still happens via the
    viewer/player path (register-on-observe), so this endpoint is an
    optimization + earlier availability, not a single point of failure.
    Body: {slug, sequence, duration, size}
    """
    _check_hook_secret(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    slug = str(body.get("slug") or "")[:64]
    try:
        seq = int(body.get("sequence"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="sequence required")
    duration = float(body.get("duration") or 0)
    size = int(body.get("size") or 0)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        raise HTTPException(status_code=400, detail="Invalid slug")
    channel = await _load_channel_by_slug(slug)
    if not channel or not channel.get("enabled", True):
        raise HTTPException(status_code=404, detail="Channel not found")
    await register_segment(slug, seq, duration=duration, size=size)
    await _touch_last_seen_live(channel["id"])
    return {"ok": True, "slug": slug, "sequence": seq}


async def _touch_last_seen_live(channel_id) -> None:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE live_channels SET last_seen_at = NOW(), status = 'LIVE' WHERE id = $1",
                channel_id,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("live last_seen update failed: %s", e)


@router.post("/shutdown")
async def mediamtx_shutdown(request: Request):
    """Stream-end webhook: publisher disconnected / path torn down.

    Body: {slug} — clears ephemeral state so the channel returns to OFFLINE
    promptly and temporary live state expires instead of lingering."""
    _check_hook_secret(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    slug = str(body.get("slug") or "")[:64]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        raise HTTPException(status_code=400, detail="Invalid slug")
    await clear_channel(slug)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE live_channels SET status = 'OFFLINE' WHERE slug = $1", slug
        )
    return {"ok": True}


@router.get("/channels/{slug}/peers")
async def channel_peer_dump(slug: str, request: Request):
    """Internal diagnostics: peers currently active for a channel. INTERNAL
    ONLY (never proxied publicly); returns peer ids only, never IPs."""
    _check_hook_secret(request)
    from ..live_state import peer_scores, peers_with_segment
    peers = await active_peers(slug)
    return {"slug": slug, "peers": peers}
