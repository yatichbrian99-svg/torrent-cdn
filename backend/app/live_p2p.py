"""Live-segment torrent identity — makes live P2P actually joinable.

The live swarm follows the VOD model: every segment (channel, seq) maps to a
single-file torrent whose infohash is SHA-1 over the bencoded info dict
{name, pieces, piece length, length}. Identity is CONTENT-derived: the backend
fetches the segment bytes from MediaMTX once, hashes them with the exact
torrentool recipe (tests/test_upload.py::TestStreamingTorrentBuilder pins the
byte format), and caches the result in Redis with the segment retention TTL.

Why the backend must vouch: a leecher needs the infohash BEFORE it has the
bytes (chicken-and-egg), so it cannot compute identity from content like a
seeder can. Serving a backend-computed infohash closes the loop:

  seeder: downloads segment from origin → create-torrent computes the SAME
          infohash from those bytes (npm bencode sorts keys, torrentool recipe
          piece lengths) → announces under it
  leecher: asks the backend for the identity → joins that exact swarm →
           BitTorrent piece verification rejects any peer serving garbage

Malicious-peer note: a peer cannot join a swarm for bytes it does not hold —
its computed infohash differs, so the tracker never introduces it. Within the
swarm, piece hashes (carried in the metadata exchanged between peers) make
poisoned segments fail verification rather than reach the player.

The verified MediaMTX segment naming (LL-HLS): variants
`<hash>_video1_stream.m3u8` carry full segments `<hash>_video1_seg<N>.mp4`.
"""

import hashlib
import json
import logging
import re
import time

import httpx

from .live_settings import get_live_settings
from .torrent_builder import bencode_info, torrentool_piece_length

logger = logging.getLogger(__name__)

_VARIANT_RE = re.compile(r"([A-Za-z0-9_]+_stream\.m3u8)")
# MediaMTX names full segments `<hex>_<stream>_seg<N>.mp4` (parts are
# `_part<k>.mp4` and are never valid swarm objects).
_SEGMENT_RE = re.compile(r"^[0-9a-f]+_video\d+_seg(\d+)\.mp4$", re.I)


def segment_filename(slug: str, seq: int) -> str:
    """The swarm's file name — MUST stay byte-identical to the frontend's
    liveSegmentInfohash()/seedSegment() name (lib/live/segments.ts)."""
    return f"{slug}-{seq}.m4s"


def infohash_from_bytes(name: str, data: bytes) -> str:
    """Infohash over the torrentool-pinned recipe (same as torrent_builder)."""
    piece_length = torrentool_piece_length(len(data))
    pieces = bytearray()
    for pos in range(0, len(data), piece_length):
        pieces += hashlib.sha1(data[pos : pos + piece_length]).digest()
    return hashlib.sha1(bencode_info(name, bytes(pieces), piece_length, len(data))).hexdigest()


async def _resolve_variant(client: httpx.AsyncClient, base: str, path: str, slug: str) -> str | None:
    """Find the channel's video variant playlist (cached in channel meta)."""
    from .live_state import get_channel_meta, set_channel_meta

    meta = await get_channel_meta(slug)
    cached = str(meta.get("hls_variant") or "")
    if cached:
        return cached
    try:
        r = await client.get(f"{base}/{path}/index.m3u8")
        if r.status_code != 200:
            return None
        variants = sorted(_VARIANT_RE.findall(r.text))
        if not variants:
            return None
        variant = next((v for v in variants if "_video1_" in v or v == "video1_stream.m3u8"), variants[0])
        await set_channel_meta(slug, hls_variant=variant)
        return variant
    except Exception:  # noqa: BLE001 — identity is best-effort
        return None


async def _fetch_segment_bytes(slug: str, seq: int) -> bytes | None:
    """Download the segment from MediaMTX via the configured HLS origin.

    In docker-compose MEDIAMTX_HLS_URL is the internal address (fast); on
    Render it is the public edge URL — the same bytes either way, fetched once
    per segment and then Redis-cached."""
    from .live_settings import get_live_settings
    from .mediamtx import mediamtx_path

    s = get_live_settings()
    base = s.mediamtx_hls_url.rstrip("/")
    path = mediamtx_path(slug)
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            variant = await _resolve_variant(client, base, path, slug)
            if not variant:
                return None
            # The variant's own playlist lists the CURRENT window; ask it for
            # the segment whose sequence number matches `seq`.
            r = await client.get(f"{base}/{path}/{variant}")
            if r.status_code != 200:
                return None
            seq_to_file: dict[int, str] = {}
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _SEGMENT_RE.match(line.split("?")[0].rsplit("/", 1)[-1])
                if m:
                    seq_to_file[int(m.group(1))] = line
            filename = seq_to_file.get(seq)
            if not filename:
                return None  # already left the live window
            seg = await client.get(f"{base}/{path}/{filename}")
            if seg.status_code != 200 or not seg.content:
                return None
            return seg.content
    except Exception as e:  # noqa: BLE001 — identity is best-effort
        logger.debug("segment fetch failed slug=%s seq=%s: %s", slug, seq, e)
        return None


async def get_segment_identity(slug: str, seq: int) -> dict | None:
    """{infohash, filename, size} for a live segment, Redis-cached.

    Returns None when the segment is gone from the live window or MediaMTX is
    unreachable — callers treat that as 'no P2P for this segment' (origin
    playback is unaffected)."""
    from .live_state import KV, _redis_call, segment_exists

    if not await segment_exists(slug, seq):
        return None
    key = f"{KV}:ih:{slug}:{seq}"
    redis = await _redis_call(lambda r: r.get(key))
    if redis:
        try:
            return json.loads(redis)
        except Exception:
            pass
    data = await _fetch_segment_bytes(slug, seq)
    if not data:
        return None
    ident = {
        "infohash": infohash_from_bytes(segment_filename(slug, seq), data),
        "filename": segment_filename(slug, seq),
        "size": len(data),
    }
    payload = json.dumps(ident)
    await _redis_call(lambda r: r.setex(key, min(s.segment_retention, 300), payload))
    return ident


def identity_now(slug: str, seq: int, data: bytes) -> dict:
    """Synchronous identity computation (tests / offline tooling)."""
    return {
        "infohash": infohash_from_bytes(segment_filename(slug, seq), data),
        "filename": segment_filename(slug, seq),
        "size": len(data),
    }
