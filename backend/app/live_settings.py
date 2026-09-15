"""Environment settings for the live-streaming MVP.

Kept in a dedicated module so the live feature stays modular. Values are read
through :func:`get_live_settings` (cached, override with ``get_live_settings.cache_clear()``
in tests) so tests can monkeypatch env vars per test — config.get_settings is
lru_cached at import time in every route module, which is exactly why conftest
patches module attributes rather than env.
"""
import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LiveSettings:
    # MediaMTX Control API (INTERNAL — bind to loopback / private network only)
    mediamtx_api_url: str          # e.g. http://mediamtx:9997
    mediamtx_api_user: str
    mediamtx_api_pass: str

    # Public URLs handed to publishers (OBS) and viewers
    mediamtx_rtmp_url: str         # e.g. rtmp://stream.example.com:1935
    mediamtx_hls_url: str          # e.g. https://stream.example.com/hls
    mediamtx_webrtc_url: str       # e.g. https://stream.example.com:8889 (optional)

    # Public playback domain (defaults to the app URL) — signed HLS URLs are
    # minted against it; the reverse proxy serves the HLS path from MediaMTX.
    public_playback_domain: str

    # Signing secret for short-lived playback tokens (derives from JWT_SECRET
    # when unset — never the reverse: a leaked playback token must not forge
    # auth sessions).
    playback_token_secret: str

    # HLS segment duration (MediaMTX must be configured to match — see
    # deploy/mediamtx.yml hlsSegmentDuration). Used for live-edge math and the
    # segment expiry defaults. 2s per the MVP latency target.
    hls_segment_duration: int

    # How long after a segment leaves the live window before its ephemeral
    # state is expired (keep = hlsPartition + margin).
    segment_retention: int

    # Heartbeat cadence the player is told to use + how long a peer stays
    # "active" after its last heartbeat (death after 3 missed beats).
    heartbeat_interval: int
    peer_timeout: int

    # Monitor cadence (channel status detection + expiry + counter rollup).
    monitor_interval: int

    # Live status is considered real only if the last segment is younger than
    # this (a stuck playlist whose segments stopped growing is OFFLINE).
    live_max_segment_age: int

    # Per-IP-per-channel viewer token budget per minute (route limiter).
    token_rate_limit: int


def get_live_settings() -> LiveSettings:
    jwt = os.environ.get("JWT_SECRET", "")
    return LiveSettings(
        mediamtx_api_url=os.environ.get("MEDIAMTX_API_URL", "http://mediamtx:9997"),
        mediamtx_api_user=os.environ.get("MEDIAMTX_API_USER", ""),
        mediamtx_api_pass=os.environ.get("MEDIAMTX_API_PASS", ""),
        mediamtx_rtmp_url=os.environ.get("MEDIAMTX_RTMP_URL", "rtmp://localhost:1935"),
        mediamtx_hls_url=os.environ.get("MEDIAMTX_HLS_URL", "http://localhost:8888"),
        mediamtx_webrtc_url=os.environ.get("MEDIAMTX_WEBRTC_URL", ""),
        public_playback_domain=os.environ.get(
            "PUBLIC_PLAYBACK_DOMAIN", ""
        ),
        playback_token_secret=os.environ.get("PLAYBACK_TOKEN_SECRET", "")
        or hashlib_sha256(f"{jwt}__live_playback__"),
        hls_segment_duration=_int_env("LIVE_HLS_SEGMENT_DURATION", 2),
        segment_retention=_int_env("LIVE_SEGMENT_RETENTION_SECONDS", 30),
        heartbeat_interval=_int_env("LIVE_HEARTBEAT_INTERVAL_SECONDS", 5),
        peer_timeout=_int_env("LIVE_PEER_TIMEOUT_SECONDS", 15),
        monitor_interval=_int_env("LIVE_MONITOR_INTERVAL_SECONDS", 5),
        live_max_segment_age=_int_env("LIVE_MAX_SEGMENT_AGE_SECONDS", 12),
        token_rate_limit=_int_env("LIVE_TOKEN_RATE_LIMIT", 60),
    )


def hashlib_sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
