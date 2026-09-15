"""Live streaming models — Pydantic schemas for the live MVP.

Kept in a dedicated module (not models.py) so the live feature stays modular
and a live regression can never touch the VOD import graph.

Tenancy: the current TorrentCDN deployment is single-tenant (the operator's
account). Every channel is owned by the creating user and stored with a
``tenant_id`` column that is pre-populated from the owner's user id — so the
data model is multi-tenant ready without inventing a separate tenancy layer
today. All queries scope by owner/tenant, and one tenant can never read or
mutate another tenant's channels (same-404 pattern as /files/{id}).
"""
import hashlib
import secrets
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

# ── Input caps (same rationale as models.py) ─────────────────────────────────
MAX_NAME_LEN = 120        # live_channels.name VARCHAR(120)
MAX_SLUG_LEN = 64         # live_channels.slug VARCHAR(64)
MAX_DESC_LEN = 500        # live_channels.description VARCHAR(500)
MAX_STREAM_KEY_LEN = 128  # live_... key transport cap (real keys are 43 chars)
MAX_SESSION_ID_LEN = 128  # viewer session id transport cap
MAX_UA_LEN = 300          # live_sessions.user_agent VARCHAR(300)
MAX_REGION_LEN = 64       # live_sessions.region VARCHAR(64)
MAX_FILENAME_LEN = 160    # live_segments.filename VARCHAR(160)

VALID_STATUSES = {"OFFLINE", "STARTING", "LIVE", "ERROR"}


def new_slug(name: str, existing: set[str] | None = None) -> str:
    """Derive a URL-safe slug from the channel name, guaranteed unique within
    ``existing`` (the tenant's current slugs) by appending -2, -3, …"""
    existing = existing or set()
    base = "".join(c.lower() if c.isalnum() else "-" for c in name)[:40].strip("-") or "channel"
    slug = base
    i = 2
    while slug in existing:
        slug = f"{base}-{i}"
        i += 1
    return slug


def new_stream_key() -> str:
    """Generate a live stream key: ``live_<43 url-safe chars>`` (~256 bits).

    Same shape/entropy philosophy as the tcdn_ API keys (secrets.token_urlsafe,
    256-bit entropy, recognizable prefix) but a separate prefix so a leaked
    key can be identified and revoked by type.
    """
    return f"live_{secrets.token_urlsafe(32)}"


def hash_stream_key(key: str) -> str:
    """SHA-256 of the full stream key — the only form stored in the DB.

    OBS sends the plaintext as the RTMP password (or ?key= token); the backend
    hashes the presented value and compares against the stored hash. A DB leak
    therefore exposes hashes, not usable publish credentials.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def stream_key_prefix(key: str) -> str:
    """Safe display prefix (never the secret). Mirrors the API-key prefix UX."""
    return key[:10] + "…"


class LiveChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    description: str | None = Field(default=None, max_length=MAX_DESC_LEN)
    slug: str | None = Field(default=None, max_length=MAX_SLUG_LEN)


class LiveChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LEN)
    description: str | None = Field(default=None, max_length=MAX_DESC_LEN)


class LiveChannelOut(BaseModel):
    """Channel as returned to the owner. Never includes the stream key hash;
    the plaintext key is only ever present in the rotate response (once)."""
    id: UUID
    tenant_id: UUID
    owner_id: UUID
    name: str
    slug: str
    description: str | None = None
    status: str
    enabled: bool
    stream_key_prefix: str | None = None
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None = None
    # Derived live metrics (Redis/live_state, not DB columns)
    current_viewers: int = 0
    current_peers: int = 0
    bitrate_kbps: float = 0.0
    uptime_seconds: int = 0
    latest_segment_sequence: int | None = None
    # One-time channel setup secrets — populated ONLY on create (and rotate);
    # list/get/detail responses leave them None so the plaintext key can never
    # be re-fetched (it is stored hashed, like the tcdn_ API keys).
    stream_key: str | None = None
    rtmp_server: str | None = None
    publish_path: str | None = None


class LiveChannelRotateResponse(BaseModel):
    """Rotate/reveal response — carries the plaintext key exactly once, plus
    the paste-ready OBS server/publish-path (both config-derived)."""
    channel_id: UUID
    stream_key: str
    stream_key_prefix: str
    rtmp_server: str | None = None
    publish_path: str | None = None


class LiveSessionCreate(BaseModel):
    """Viewer join. ``session_id`` is an opaque client-generated id (no PII)."""
    channel_slug: str = Field(min_length=1, max_length=MAX_SLUG_LEN)
    session_id: str = Field(min_length=8, max_length=MAX_SESSION_ID_LEN)
    user_agent: str | None = Field(default=None, max_length=MAX_UA_LEN)
    region: str | None = Field(default=None, max_length=MAX_REGION_LEN)


class LiveHeartbeat(BaseModel):
    channel_slug: str = Field(min_length=1, max_length=MAX_SLUG_LEN)
    session_id: str = Field(min_length=8, max_length=MAX_SESSION_ID_LEN)
    peer_id: str | None = Field(default=None, max_length=MAX_SESSION_ID_LEN)
    bytes_uploaded: int = Field(default=0, ge=0, le=10 * 1024**3)
    bytes_downloaded: int = Field(default=0, ge=0, le=10 * 1024**3)
    segments_served: int = Field(default=0, ge=0, le=10**9)
    segments_failed: int = Field(default=0, ge=0, le=10**9)
    buffering_events: int = Field(default=0, ge=0, le=10**9)
    playback_errors: int = Field(default=0, ge=0, le=10**9)
    current_segment: int | None = Field(default=None, ge=0)
    current_position: float | None = Field(default=None, ge=0)


class LiveSegmentAvailability(BaseModel):
    """Segment-availability report from a viewer's swarm.

    ``haves`` names the sequence numbers the local peer POSSESSES (it will
    serve them to others via WebRTC); ``transfers`` reports per-segment
    delivery outcomes so backend counters stay measured, never modeled."""
    channel_slug: str = Field(min_length=1, max_length=MAX_SLUG_LEN)
    session_id: str = Field(min_length=8, max_length=MAX_SESSION_ID_LEN)
    peer_id: str = Field(min_length=8, max_length=MAX_SESSION_ID_LEN)
    haves: list[int] = Field(default_factory=list, max_length=50)
    transfers: list["LiveSegmentTransfer"] = Field(default_factory=list, max_length=50)


class LiveSegmentTransfer(BaseModel):
    sequence_number: int = Field(ge=0)
    served: bool
    bytes: int = Field(default=0, ge=0, le=1024 * 1024 * 1024)


class LiveSessionOut(BaseModel):
    session_id: str
    channel_slug: str
    live: bool
    latest_sequence: int | None = None
    live_edge_sequence: int | None = None
    latency_target_seconds: int = 2
    swarm_base: str = ""


class LivePlaybackInfo(BaseModel):
    """Playback descriptor handed to the live player. ``token`` authenticates
    subsequent viewer API calls (sessions/heartbeats) for private channels —
    the stream key itself is never involved in playback."""
    channel_id: UUID
    channel_slug: str
    channel_name: str
    status: str
    hls_url: str
    token: str
    swarm_base: str
    latency_target_seconds: int = 2
    # Paste-ready OBS ingest (owner-visible; no secrets — config-derived hosts).
    rtmp_server: str | None = None
    publish_path: str | None = None
