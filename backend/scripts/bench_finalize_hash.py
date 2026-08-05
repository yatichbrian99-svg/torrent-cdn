#!/usr/bin/env python3
"""Benchmark the upload-finalize hashing change: streaming hash vs torrentool re-read.

The presign-finalize path used to download the object to disk, then torrentool
re-read the WHOLE file a second time to SHA-1 its pieces. It now hashes the
bytes WHILE they arrive (hash_pieces_from_stream), so the hashing is hidden
inside the download instead of being a sequential pass after it.

This script measures, on a local file of a given size:

  1. t_new  — hash_pieces_from_stream reading the file in chunks (the cost of
              the hashing that now OVERLAPS the download; the network read is
              the dominant, unavoidable term on both sides).
  2. t_old  — torrentool's Torrent.create_from: the full re-read + SHA-1 pass
              that the old code ran SEQUENTIALLY after the download finished.
  3. Simulated wall-clock at a few download rates (the download term is shared,
     so the wall-clock saving of the new flow is exactly the sequential pass).

Local CPU numbers are a lower bound for Render's throttled free tier, where the
same pass costs several times longer.

Usage:  python scripts/bench_finalize_hash.py [size_mb]   (default 200)
"""
import hashlib
import os
import sys
import time

from torrentool.api import Torrent

from app.torrent_builder import (
    bencode_info,
    hash_pieces_from_stream,
    torrentool_piece_length,
)

MB = 1024 * 1024
SIZE_MB = int(sys.argv[1]) if len(sys.argv) > 1 else 200
PATH = f"/tmp/bench_finalize_{SIZE_MB}mb.dat"
CHUNK = 5 * MB


def make_file(size: int) -> int:
    if not os.path.exists(PATH) or os.path.getsize(PATH) != size:
        print(f"generating {size // MB} MiB test file ...", flush=True)
        with open(PATH, "wb") as f:
            f.write(os.urandom(size))
    return os.path.getsize(PATH)


def time_best(fn, n: int = 3) -> float:
    best = None
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        best = dt if best is None or dt < best else best
    return best


def stream_hash() -> bytes:
    with open(PATH, "rb") as fh:
        return hash_pieces_from_stream(lambda: fh.read(CHUNK), size)


def old_hash() -> str:
    return str(Torrent.create_from(PATH).info_hash)


size = make_file(SIZE_MB * MB)

# Warm the page cache once so both reads are cache-hot (as in production,
# where the just-downloaded file is in the OS cache).
with open(PATH, "rb") as fh:
    while fh.read(CHUNK):
        pass

t_new = time_best(stream_hash)
t_old = time_best(old_hash)

pieces = stream_hash()
new_btih = hashlib.sha1(
    bencode_info(os.path.basename(PATH), pieces, torrentool_piece_length(size), size)
).hexdigest()
old_btih = old_hash()

print(f"\nfile: {size // MB} MiB ({PATH})")
print(f"  new streaming hash during download : {t_new * 1000:7.0f} ms")
print(f"  old torrentool re-read + hash pass : {t_old * 1000:7.0f} ms")
print(f"  sequential pass eliminated          : {(t_old - t_new) * 1000:7.0f} ms")
print(f"  infohash identical (no swarm fork)  : {new_btih == old_btih}")

print("\n  simulated wall-clock at a few download rates "
      "(download term shared by both flows):")
print(f"  {'rate':>6}  {'new (hash hidden in download)':>28}  {'old (download + re-read)':>26}")
for rate_mbps in (10, 20, 50, 100):
    dl = size / (rate_mbps * MB)  # seconds at this download rate
    print(f"  {rate_mbps:>5}MB/s  {dl:>17.2f}s  {dl + t_old:>17.2f}s  (saves {t_old * 1000:.0f} ms)")

os.remove(PATH)
print("\ndone — cleaned up", PATH)
