"""Streaming torrent infohash builder.

Computes the SHA-1 piece hashes of a file WHILE its bytes stream past (e.g. the
Supabase download in the presign-finalize path), then builds the magnet link
directly — so the file is never re-read just to hash it.

The infohash must stay BYTE-IDENTICAL to what torrentool's
``Torrent.create_from`` produced before (and to what the browser-side P2P
seeder rebuilds from the magnet), otherwise the swarm forks and peers can never
find each other. The recipe is pinned by
``tests/test_upload.py::TestStreamingTorrentBuilder``:

- piece length: 32768 when size <= 32768, else 262144
  (torrentool/torrent.py: size_min=32768, size_default=262144)
- pieces: concatenated SHA-1 digests, one per piece of exactly piece_length
  bytes, plus a trailing partial piece
- info dict keys: name, pieces, piece length, length — bencoded with the keys
  SORTED (torrentool's Bencode.encode sorts dict keys), giving the byte order
  ``length, name, piece length, pieces``
- infohash: sha1 over that bencoded info dict
"""

import hashlib
import urllib.parse

# Public WebTorrent WSS trackers baked into every magnet. fastcast.nz and
# tracker.btorrent.xyz are dead/unreliable (TCP connects fail / live WS 500s —
# the embed suite kept logging btorrent's WebSocket 500 while peers still
# connected fine through the other two). The magnet MUST carry them as tr=
# params: webtorrent's client has NO default tracker list and only announces
# to the magnet's `tr`, so without them the embed player could never discover
# peers and P2P would silently never engage.
#
# THREE trackers, not two: peer discovery is the swarm's single point of
# failure at scale — a tracker that rate-limits or drops WebSocket connects
# during a traffic spike leaves new viewers with no peers (the collapse
# scenario in scripts/swarm_collapse_sim.py). A third independent tracker
# means discovery survives any ONE tracker going away. All three are live
# openwebtorrent-compatible WSS trackers; clients announce to every tr= and
# union the peer sets, so the third is pure redundancy. NOTE: adding a
# tracker does NOT change the infohash (announce list is not part of the
# info dict), so existing swarms keep working while gaining a third
# discovery path.
PUBLIC_TRACKERS = [
    "wss://tracker.webtorrent.dev",
    "wss://tracker.openwebtorrent.com",
    "wss://tracker.files.fm:7073/announce",
]


def torrentool_piece_length(size: int) -> int:
    """torrentool's piece-length rule (torrentool/torrent.py: size_min=32768,
    size_default=262144). The frontend seeder mirrors this exactly."""
    return 262144 if size > 32768 else 32768


def hash_pieces_from_stream(reader, file_size: int) -> bytes:
    """Read the whole stream via ``reader`` (a callable returning bytes chunks,
    ``b''`` at EOF) and return the concatenated SHA-1 piece digests.

    Piece boundaries are exact regardless of the reader's chunk size (a chunk
    may span multiple pieces, and a piece may span multiple chunks) — verified
    byte-identical to torrentool for chunk sizes from 3 bytes to 1 MiB.
    """
    piece_length = torrentool_piece_length(file_size)
    pieces = bytearray()
    buf = bytearray()
    read_any = False
    while True:
        chunk = reader()
        if not chunk:
            break
        read_any = True
        if not buf and len(chunk) >= piece_length:
            # Fast path: digest whole pieces straight from the chunk — zero
            # copies (memoryview slice + hashlib buffer protocol). This is the
            # common case: the download/upload readers return CHUNK_SIZE reads
            # far larger than a piece, so no per-chunk buffering copy is needed.
            pos = 0
            while len(chunk) - pos >= piece_length:
                pieces += hashlib.sha1(memoryview(chunk)[pos:pos + piece_length]).digest()
                pos += piece_length
            if pos:
                chunk = chunk[pos:]  # trailing partial piece
        buf += chunk
        while len(buf) >= piece_length:
            # Offset-free flush: memoryview slice = zero-copy, and each `del`
            # shifts at most one partial piece (never an O(n^2) memmove).
            pieces += hashlib.sha1(memoryview(buf)[:piece_length]).digest()
            del buf[:piece_length]
    if buf and read_any:
        pieces += hashlib.sha1(memoryview(buf)).digest()
    return bytes(pieces)


def bencode_info(name: str, pieces: bytes, piece_length: int, length: int) -> bytes:
    """Bencode the single-file info dict EXACTLY as torrentool does.

    torrentool's Bencode.encode sorts dict keys alphabetically, so the emitted
    order is ``length, name, piece length, pieces``. A byte difference here
    would fork the swarm — this ordering must never be 'cleaned up'.
    """
    name_b = name.encode("utf-8")
    return (
        b"d"
        + b"6:length"
        + f"i{length}e".encode()
        + b"4:name"
        + f"{len(name_b)}:".encode()
        + name_b
        + b"12:piece length"
        + f"i{piece_length}e".encode()
        + b"6:pieces"
        + f"{len(pieces)}:".encode()
        + pieces
        + b"e"
    )


def build_magnet(name: str, pieces: bytes, length: int) -> str:
    """Build the full magnet link (xt + dn + tr= trackers) for a single file.

    ``name`` must equal the torrent's info-dict name — the callers write the
    temp file as ``safe_filename`` (never mkstemp's random prefix) so this
    holds, keeping the magnet's dn and the reproducible infohash correct.
    """
    piece_length = torrentool_piece_length(length)
    info = bencode_info(name, pieces, piece_length, length)
    info_hash = hashlib.sha1(info).hexdigest()
    tr_params = "".join(f"&tr={urllib.parse.quote(u, safe='')}" for u in PUBLIC_TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={name}{tr_params}"


def with_webseed(magnet: str, webseed_url: str) -> str:
    """Append a BEP 19 webseed (``ws=``) parameter to a magnet link.

    WebTorrent treats an http(s) ``ws`` param as a WebSeed: a permanent
    full-file peer it fetches pieces from via HTTP Range requests (BEP 19 —
    ``206`` / ``Content-Range``) whenever the swarm can't supply them. The
    origin object URL is the natural webseed — it always holds the file and
    never leaves the swarm — so thin or collapsed swarms are backed up by
    origin at P2P price instead of the whole-file direct fallback.

    The infohash is the SHA-1 over the torrent's INFO DICT ONLY, so appending
    ``ws=`` never changes the infohash and never forks the swarm (the frontend
    seeder rebuilds the identical info dict from xt/dn/tr and is unaffected).

    ``webseed_url`` must be an http(s) URL; anything else (or empty) leaves the
    magnet unchanged. Idempotent: a magnet that already carries ``ws=`` is
    returned untouched (dedup can reuse a stored copy whose magnet may already
    have one).
    """
    if not webseed_url:
        return magnet
    parsed = urllib.parse.urlparse(webseed_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return magnet
    if "ws=" in magnet:
        return magnet
    ws = urllib.parse.quote(webseed_url, safe="")
    return f"{magnet}&ws={ws}"
