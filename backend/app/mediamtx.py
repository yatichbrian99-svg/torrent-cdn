"""MediaMTX control-API client + playback token signing.

MediaMTX owns ALL media: RTMP ingest, HLS muxing, protocol conversion. This
client only asks it questions (is the path live? what are its tracks/RGB
details?) and never reimplements media handling. The Control API is INTERNAL
(loopback / private network) and never proxied publicly.

Auth contract (verified against MediaMTX v1.x docs):
- Control API: HTTP Basic auth (apiUser/apiPass) when configured.
- External HTTP auth hook: MediaMTX POSTs a JSON body with
  user/password/token/ip/action/path/protocol/query/userAgent to the backend's
  /api/internal/mediamtx/auth (see routes/live_internal.py) and expects 2xx to
  allow. Older versions used GET with query params — the POST JSON shape is
  the current documented contract (authMethod: http).
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import quote

import httpx

from .live_settings import get_live_settings

logger = logging.getLogger(__name__)

# ONE path namespace everywhere: the RTMP publish `live/<slug>` IS the
# MediaMTX path name — RTMP publishes, the auth hook, and HLS playback URLs
# all resolve against it. (Verified live: OBS publishing to
# rtmp://host:1935/live/<slug> creates MediaMTX path "live/<slug>", and HLS
# is served at /live/<slug>/index.m3u8 — a slug-only HLS URL 404s.)
def mediamtx_path(slug: str) -> str:
    return f"live/{slug}"


def rtmp_ingress_path(slug: str) -> str:
    """The RTMP application/stream name OBS publishes to (publish path)."""
    return f"live/{slug}"


# ── Playback tokens (short-lived, HMAC-signed, stateless) ────────────────────

def _sign(payload_b64: str) -> str:
    secret = get_live_settings().playback_token_secret
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def mint_playback_token(channel_id: str, ttl_seconds: int = 300) -> str:
    """Mint a short-lived playback token for a channel.

    Stateless (HMAC-signed, DB-free verification) so token checks never hit
    the database — the viewer hot path checks a token on every playlist poll.
    Payload: {c: channel_id, e: expiry_epoch}. The token authorizes PLAYBACK
    and viewer live API calls for that channel only — never publishing.
    """
    payload = json.dumps({"c": str(channel_id), "e": int(time.time()) + ttl_seconds},
                         separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = _sign(payload_b64)
    return f"{payload_b64}.{sig}"


def verify_playback_token(token: str, channel_id: str) -> bool:
    """Verify a playback token against the channel. Constant-time signature
    compare; expired tokens fail closed. Invalid shapes return False (never
    raise — the hot path calls this on every playlist cycle)."""
    try:
        payload_b64, sig = token.split(".", 1)
        expected = _sign(payload_b64)
        if not hmac.compare_digest(sig, expected):
            return False
        pad = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
        if payload.get("c") != str(channel_id):
            return False
        return int(payload.get("e", 0)) > time.time()
    except Exception:
        return False


# ── Public URL builders (config-driven — never hardcode hosts) ───────────────

def hls_url(slug: str) -> str:
    """Public HLS playlist URL for a channel (served through the reverse proxy
    with live-appropriate cache headers — see Caddyfile)."""
    base = get_live_settings().mediamtx_hls_url.rstrip("/")
    return f"{base}/{quote(mediamtx_path(slug))}/index.m3u8"


def rtmp_publish_url(slug: str) -> str:
    """The RTMP server URL to paste into OBS (without the stream key)."""
    base = get_live_settings().mediamtx_rtmp_url.rstrip("/")
    app = rtmp_ingress_path(slug).rsplit("/", 1)[0]
    return f"{base}/{app}"


def playback_origin_base(slug: str) -> str:
    """Origin base for viewer-side segment fetching (the same host as the HLS
    URL). Used by the player loader as the fallback fetcher for P2P misses."""
    return hls_url(slug).rsplit("/", 1)[0]


# ── Control API (read-only usage) ────────────────────────────────────────────

async def list_paths() -> dict[str, dict]:
    """GET /v3/paths/list → {items: [{name, ready, source, tracks, ...}]}.

    Returns {} on ANY failure (MediaMTX down, auth misconfigured) — callers
    treat that as "status unknown" and fall back to segment-registry state.
    """
    s = get_live_settings()
    if not s.mediamtx_api_url:
        return {}
    auth = ""
    if s.mediamtx_api_user:
        raw = f"{s.mediamtx_api_user}:{s.mediamtx_api_pass}"
        auth = base64.b64encode(raw.encode()).decode()
    headers = {"Authorization": f"Basic {auth}"} if auth else {}
    url = s.mediamtx_api_url.rstrip("/") + "/v3/paths/list"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug("mediamtx paths/list -> %s", resp.status_code)
                return {}
            data = resp.json()
            return {item.get("name", ""): item for item in (data.get("items") or [])}
    except Exception as e:  # noqa: BLE001 — MediaMTX down must not 500 the API
        logger.debug("mediamtx paths/list failed: %s", e)
        return {}


async def get_path(slug: str) -> dict | None:
    paths = await list_paths()
    path = paths.get(mediamtx_path(slug))
    return path or None


async def get_path_by_ingress(slug: str) -> dict | None:
    """Resolve a channel's MediaMTX path when the publish path was
    live/<slug> (RTMP strips the app prefix into the path name)."""
    path, _authoritative = await resolve_ingress(slug)
    return path


async def resolve_ingress(slug: str) -> tuple[dict | None, bool]:
    """Resolve a channel's MediaMTX path AND whether the answer is authoritative.

    Returns (path_or_None, authoritative):
      authoritative=True  → MediaMTX answered; None means "no such path"
                            (the publisher is genuinely disconnected)
      authoritative=False → MediaMTX unreachable or unconfigured; stream state
                            is UNKNOWN and callers should fall back to the
                            segment registry / last_seen heuristics.
    """
    paths = await list_paths()
    if not paths:
        return None, False
    return (paths.get(mediamtx_path(slug))
            or paths.get(rtmp_ingress_path(slug))
            or paths.get(slug)), True


async def mediamtx_reachable() -> bool:
    """Cheap liveness probe for /api/health/live."""
    s = get_live_settings()
    if not s.mediamtx_api_url:
        return False
    auth = ""
    if s.mediamtx_api_user:
        raw = f"{s.mediamtx_api_user}:{s.mediamtx_api_pass}"
        auth = base64.b64encode(raw.encode()).decode()
    headers = {"Authorization": f"Basic {auth}"} if auth else {}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(s.mediamtx_api_url.rstrip("/") + "/v3/paths/list",
                                    headers=headers)
            return resp.status_code == 200
    except Exception:
        return False
