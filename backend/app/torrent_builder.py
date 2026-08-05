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

# Public WebTorrent WSS trackers baked into every magnet. fastcast.nz is
# defunct (TCP connects fail) — this trio is the canonical, currently-live set.
# The magnet MUST carry them as tr= params: webtorrent's client has NO default
# tracker list and only announces to the magnet's `tr`, so without them the
# embed player could never discover peers and P2P would silently never engage.
PUBLIC_TRACKERS = [
    "wss://tracker.webtorrent.dev",
    "wss://tracker.openwebtorrent.com",
    "wss://tracker.btorrent.xyz",
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
    while True:
        chunk = reader()
        if not chunk:
            break
        buf += chunk
        while len(buf) >= piece_length:
            pieces += hashlib.sha1(bytes(buf[:piece_length])).digest()
            del buf[:piece_length]
    if buf:
        pieces += hashlib.sha1(bytes(buf)).digest()
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
