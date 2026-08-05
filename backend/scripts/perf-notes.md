# Perf notes — finalize torrent-hashing (streaming vs torrentool re-read)

Measured with `scripts/bench_finalize_hash.py [size_mb]` on a local dev box
(2026-08-05, optimized hasher from commit `f156864`). The script generates a
random file, times **best-of-3** for each path on a warm page cache, verifies
the infohash is byte-identical between the two builders, and prints simulated
wall-clocks at common download rates.

## What is being compared

The presign-finalize path downloads the object from Supabase Storage, then has
to SHA-1 its pieces to build the magnet's infohash.

- **Old flow:** download to disk, *then* `Torrent.create_from` re-reads the
  whole file and hashes it — a second full pass, sequential after the download.
- **New flow:** `torrent_builder.hash_pieces_from_stream` hashes the bytes
  *while they arrive* from the download — the pass still runs, but it overlaps
  the network read instead of extending finalize.

The download term is shared by both flows, so the wall-clock saving of the new
flow is exactly the old sequential pass.

## Scaling numbers (local machine)

| File size | New streaming hash | Old torrentool re-read+hash | Delta in isolation | Wall-clock saved per upload |
|---|---|---|---|---|
| 50 MiB  | 84 ms   | 80 ms   | −4 ms (noise)   | ~80 ms   |
| 200 MiB | 381 ms  | 323 ms  | −58 ms (noise)  | ~323 ms  |
| 1000 MiB| 1712 ms | 1631 ms | −81 ms (noise)  | ~1631 ms |

- **Infohash byte-identical at every size** (asserted by the script; also pinned
  by `TestStreamingTorrentBuilder` oracle tests).
- The optimized hasher sits at **parity with torrentool in isolation** (≤5%
  gap, within run-to-run noise). An earlier draft was 571 ms @ 200 MiB — the
  zero-copy `memoryview` fast path (commit `f156864`) closed that gap.
- Scaling is linear: ~1.7 µs per byte for both paths (84 ms/50 MiB ≈
  1.68 µs/B; 1712 ms/1000 MiB ≈ 1.71 µs/B).

## What this means in production

- At the 1 GB plan max the old flow paid a **~1.6 s sequential hash pass**
  (local CPU) that the new flow hides inside the download. On Render's
  throttled free-tier CPU the same pass costs several times longer, so the
  saving is larger there.
- Supabase Storage currently caps objects at 50 MiB per bucket, so today's
  per-upload saving is ~0.1 s; raising the cap is what makes the streaming
  design pay off at scale.
- The hashing is intentionally NOT the bottleneck of finalize: the VirusTotal
  blocking scan (≤32 MiB files) and the server-side download dominate.

## Re-running

```bash
cd backend
PYTHONPATH=. ./.venv/bin/python scripts/bench_finalize_hash.py 200    # 200 MiB
PYTHONPATH=. ./.venv/bin/python scripts/bench_finalize_hash.py 1000   # 1 GiB
```

Update this table whenever the hasher or the benchmark changes.
