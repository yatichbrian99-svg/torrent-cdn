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

# Slug pattern shared by every internal hook body.
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

# Ingest webhook shared secret (MEDIAMTX_HOOK_SECRET env — MediaMTX sends it
# in the X-Hook-Secret header / ?secret= query). Optional but recommended when
# the hook is reachable from anything but the MediaMTX container.
_HOOK_SECRET = None


def _hook_secret() -> str:
    global _HOOK_SECRET
    if _HOOK_SECRET is None:
        _HOOK_SECRET = os.environ.get("MEDIAMTX_HOOK_SECRET", "")
    return _HOOK_SECRET


_HOOK_BAD_WINDOW = 10  # seconds — matches the "try again" window we report
_HOOK_BAD_LIMIT = 5  # bad-secret attempts per window per IP (incl. MediaMTX retries)
_hook_bad_attempts: dict[str, list[float]] = {}

# Publisher lifecycle tracking, keyed by slug (ephemeral by design — the
# monitor's clear_channel wipes it on stream end; bounded like the limiter).
# Values: publish_first_seen (epoch), publish_attempts count (incl. retries),
# publish_denied count, last_publish_result, last_disconnect epoch.
_publish_lifecycle: dict[str, dict[str, float]] = {}
_PUBLISH_LIFECYCLE_MAX = 200


def _track_publish_event(slug: str, outcome: str) -> None:
    """Record a publish auth attempt (ok / denied / challenge) for the
    diagnostics endpoint + reconnect counting. Never stores the key itself."""
    st = _publish_lifecycle.setdefault(slug, {})
    now = time.time()
    if outcome == "ok" and not st.get("publish_first_seen"):
        st["publish_first_seen"] = now  # reconnects do NOT reset the first-seen
    if outcome in {"ok", "denied", "challenge"}:
        st["publish_attempts"] = st.get("publish_attempts", 0) + 1
    if outcome == "denied":
        st["publish_denied"] = st.get("publish_denied", 0) + 1
    st["last_publish_result"] = now if outcome != "challenge" else st.get("last_publish_result", 0)
    st["last_publish_outcome"] = {"ok": "accepted", "denied": "denied",
                                  "challenge": "challenge"}.get(outcome, outcome)
    while len(_publish_lifecycle) > _PUBLISH_LIFECYCLE_MAX:
        _publish_lifecycle.pop(next(iter(_publish_lifecycle)))


def _track_disconnect(slug: str) -> None:
    st = _publish_lifecycle.setdefault(slug, {})
    st["last_disconnect"] = time.time()
    st.pop("publish_first_seen", None)  # next connect starts a fresh uptime


def publish_lifecycle_snapshot(slug: str) -> dict[str, float | str | None]:
    """Read-only view for the diagnostics endpoint (no secrets, no IPs)."""
    st = _publish_lifecycle.get(slug) or {}
    return {
        "publish_first_seen": st.get("publish_first_seen"),
        "publish_attempts": int(st.get("publish_attempts", 0)),
        "publish_denied": int(st.get("publish_denied", 0)),
        "last_publish_outcome": st.get("last_publish_outcome"),
        "last_disconnect": st.get("last_disconnect"),
    }


def _check_hook_secret(request: Request) -> None:
    secret = _hook_secret()
    if not secret:
        return  # not configured — rely on network isolation
    presented = request.headers.get("x-hook-secret") or ""
    if not presented:
        presented = (request.query_params.get("secret") or "")
    if presented == secret:
        return
    # Wrong/missing secret: tightly rate-limit INSTEAD of failing instantly on
    # the first bad attempt. MediaMTX retries its auth hook aggressively on
    # startup/reconnect, and publishers behind NAT or a shared egress (bore.pub,
    # cloud relays) all present the same source IP — an instant 403 storm from
    # one retry loop would lock every publisher behind that IP out of MediaMTX
    # (and the failure surfaces to OBS as an inscrutable generic RTMP error).
    # A valid secret bypasses this entirely; the auth logic itself stays strict.
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in _hook_bad_attempts.get(ip, []) if now - t < _HOOK_BAD_WINDOW]
    if len(recent) >= _HOOK_BAD_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Try again in {int(_HOOK_BAD_WINDOW - (now - recent[0])) or 1}s.",
        )
    recent.append(now)
    _hook_bad_attempts[ip] = recent
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

    Publish: the channel slug comes from the path (`live/<slug>`) and the
    stream key from `password` — which MediaMTX sources from the stream-name
    QUERY STRING (`?user=<slug>&pass=<key>`). Verified empirically: RTMP URL
    userinfo is NOT forwarded (user arrives empty) because RTMP handshake has
    no credential phase; MediaMTX only parses `user`/`pass` out of the query.
    So OBS setup is: Server = rtmp://host:1935/live, Stream key =
    `<slug>?pass=<stream key>` — verified against the stored SHA-256 hash
    (never logged, constant-time). Empty credentials → 401 challenge, which
    is how MediaMTX surfaces a missing key to the publisher.
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
        logger.warning("live_ingest %s REJECTED path=%s (unmappable)", action or "?", path[:80])
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    channel = await _load_channel_by_slug(slug)
    if not channel or not channel.get("enabled", True):
        # 404-style rejection: do not reveal whether the channel exists.
        if action == "publish":
            _track_publish_event(slug, "denied")
        logger.warning("live_ingest %s REJECTED slug=%s (missing-or-disabled)", action, slug)
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    if action == "publish":
        presented = password or token or user
        presented_hash = hash_stream_key(presented)
        stored = channel.get("stream_key_hash") or ""
        import hmac as _hmac

        if not stored or not _hmac.compare_digest(presented_hash, stored):
            _track_publish_event(slug, "denied")
            # Structured, secret-free: outcome + identifiers only. The presented
            # key/hash must NEVER appear here (spec: no secrets in logs).
            logger.warning(
                "live_ingest publish DENIED slug=%s protocol=%s ip=%s path=%s",
                slug, protocol, request.client.host if request.client else "-", path,
            )
            record_event("live_publish_denied", detail=f"slug={slug}")
            return JSONResponse(status_code=403, content={"detail": "forbidden"})
        # Authorized publish → STARTING marker; the first segment flips it to LIVE.
        reconnect = bool(_publish_lifecycle.get(slug, {}).get("last_disconnect"))
        _track_publish_event(slug, "ok")
        await set_channel_meta(slug, publish_seen=str(time.time()))
        await _touch_last_seen(channel["id"])
        # Structured lifecycle log: one line per accepted publish attempt.
        logger.info(
            "live_ingest publish ACCEPTED slug=%s protocol=%s reconnect=%s path=%s",
            slug, protocol, reconnect, path,
        )
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
    await register_segment(slug, seq, duration=duration, size=size,
                           codec=body.get("codec"), bitrate_kbps=body.get("bitrate_kbps"),
                           resolution=body.get("resolution"), fps=body.get("fps"))
    await _touch_last_seen_live(channel["id"])
    logger.info("live_ingest segment slug=%s seq=%s size=%s", slug, seq, size)
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
    _track_disconnect(slug)
    logger.info("live_ingest stream END slug=%s", slug)
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


# ── runOnReady / runOnNotReady hooks (MediaMTX path lifecycle) ────────────────
# MediaMTX executes these as one-shot shell commands with MTX_PATH in the
# environment the moment a stream becomes (or stops being) readable. The
# command POSTs here — a secret-free, secret-protected notification that is
# FAR faster than waiting for the monitor's playlist poll to notice segments.


def _hook_form(body: bytes) -> dict:
    """Parse the application/x-www-form-urlencoded body the hook command sends
    (curl --data-urlencode). Raises ValueError on a malformed body."""
    from urllib.parse import parse_qs
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


@router.post("/ready")
async def mediamtx_ready(request: Request):
    """Stream-ready webhook (MediaMTX runOnReady hook).

    Fired by MediaMTX the instant a stream becomes readable — typically
    <1s after the publisher's first frame, versus up to one monitor interval
    (default 5s) + mux latency for playlist-poll discovery. Marks the channel
    confirmed (LIVE) immediately and pre-warms the playlist poll so segments
    register on the next tick.

    Body: form-encoded slug=<name> (sent by the hook command). Protected by
    the shared hook secret (header/query, same as /ingest and /shutdown)."""
    _check_hook_secret(request)
    raw = await request.body()
    try:
        form = _hook_form(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid body")
    # MTX_PATH is the full MediaMTX path name ("live/<slug>") — normalize
    # exactly like the auth hook does. Accepts a bare slug too.
    slug = _slug_from_path(str(form.get("slug") or "")) or ""
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid slug")
    channel = await _load_channel_by_slug(slug)
    if not channel or not channel.get("enabled", True):
        logger.warning("live_ingest ready REJECTED slug=%s (missing-or-disabled)", slug)
        raise HTTPException(status_code=404, detail="Channel not found")
    await set_channel_meta(slug, publish_seen=str(time.time()))
    await _touch_last_seen_live(channel["id"])
    logger.info("live_ingest stream READY slug=%s (mediamtx confirmed)", slug)
    record_event("live_publish_ok", detail=f"slug={slug} ready")
    # Nudge segment registration: the HLS muxer may have produced the first
    # segment already; the monitor picks the rest up on its next tick.
    from ..live_state import _register_segments_from_playlist
    try:
        await _register_segments_from_playlist(slug)
    except Exception as e:  # noqa: BLE001 — the webhook must never fail the hook
        logger.debug("ready-hook playlist registration failed for %s: %s", slug, e)
    return {"ok": True, "slug": slug}


@router.post("/not-ready")
async def mediamtx_not_ready(request: Request):
    """Stream-end webhook (MediaMTX runOnNotReady hook).

    Same contract as /shutdown: clears ephemeral state so the channel returns
    to OFFLINE promptly. The hook command posts form-encoded slug=<name>.
    Best-effort by design — a lost notification changes nothing (the monitor
    still expires state; the hook only makes OFFLINE arrive in ~1s instead of
    after segment expiry)."""
    _check_hook_secret(request)
    raw = await request.body()
    try:
        form = _hook_form(raw)
    except Exception:
        # Some MediaMTX restart paths can kill the command mid-write; a
        # truncated/absent body must NOT 400 (MediaMTX would then treat the
        # hook as failed and log noise on every stream end).
        form = {}
    # Normalize like the auth hook (MTX_PATH is "live/<slug>").
    slug = _slug_from_path(str(form.get("slug") or "")) or ""
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid slug")
    _track_disconnect(slug)
    logger.info("live_ingest stream END slug=%s (mediamtx not-ready)", slug)
    await clear_channel(slug)
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE live_channels SET status = 'OFFLINE' WHERE slug = $1", slug
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("not-ready status update failed: %s", e)
    return {"ok": True}
