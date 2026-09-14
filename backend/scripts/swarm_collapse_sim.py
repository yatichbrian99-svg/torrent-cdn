#!/usr/bin/env python3
"""Swarm-collapse simulation for the TorrentCDN P2P delivery model.

Models viewers streaming a single file through WebTorrent and measures how many
bytes actually leave the origin (billed) vs. flow peer-to-peer (free). The
point is to quantify what a *collapsed* swarm costs — the scenario where the
early viewers who hold the tail of the file leave, or a whole wave ends, and
the remaining/new viewers have nobody to pull the missing pieces from — and
what an HTTP **webseed** changes about that cost.

Why the model looks the way it does (simplifications, stated honestly):

- **Sequential streaming.** Viewers pull pieces in play order (the <video>
  element requests data as it plays, and WebTorrent prioritizes the pieces
  being read). So a viewer at progress x holds the prefix [0, x] — NOT a
  random spread. This is the structural weakness that makes swarms fragile:
  everyone has the same early pieces, few people hold the tail.

- **Leading edge / stall.** A viewer can only download via P2P while SOME
  connected peer holds a piece beyond their current position. If nobody is
  ahead of them (swarm empty, or every tail-holder left), the next byte must
  come from the origin — the player's 5s fallback. These are **stall bytes**:
  origin egress that exists ONLY because the swarm couldn't serve.

- **Bandwidth gap.** Even with peers ahead, their aggregate upload is finite
  (asymmetric home connections: 1–10 Mbps per browser is the honest range).
  Demand beyond that pool is served by origin. These are **gap bytes** — the
  minimum origin egress the design costs even with a perfect swarm.

- **Webseed.** An HTTP(S) seed URL in the magnet (BEP 19 / WebTorrent `ws=`)
  acts as a PERMANENT full-file peer with its own upload bandwidth. It never
  leaves the swarm, so the stall path disappears entirely (there is always a
  peer ahead). Origin egress is then bounded by the bandwidth gap, and if the
  webseed's own bandwidth is under-provisioned the viewers are throttled —
  exactly like a real origin under load. Webseed bytes are still origin egress
  (billed), but only the gap — never the whole file per viewer.

- **Churn.** A viewer seeds only while their tab is open. When they leave,
  every byte they held leaves the swarm with them.

- **Complexity.** "Is any peer ahead of me?" is answered in O(1) via a
  *running frontier* — the max progress across the swarm, updated as each
  viewer advances (so a viewer processed earlier in the same tick counts as
  ahead of later ones, exactly like the original per-viewer O(N) scan, which
  made 200k-viewer runs impractically slow). With a webseed the frontier is
  always +inf: a full copy never leaves.

Modes:

  --mode steady     Viewers arrive continuously; sessions overlap. The healthy
                    baseline.
  --mode collapse   TWO waves. Wave 1 fills the swarm, everyone watches to
                    watch-pct then leaves in a short window. Wave 2 arrives
                    after the swarm has emptied → with no webseed, 100% origin
                    until a new full-holder appears; with a webseed, the swarm
                    is never empty and origin only covers the gap.
  --mode tail-gap   ONE burst. Viewers stream sequentially; the early arrivals
                    (who hold the tail) leave at watch-pct while later viewers
                    are still mid-file. The leading edge collapses; everyone
                    behind it fills the gap from origin.
  --mode cold       Viewers arrive with sessions shorter than the gap between
                    arrivals — the swarm rarely has two people at once.

Bandwidth: with --unlimited-p2p (default) P2P transfers are assumed fast
whenever a peer ahead exists — the model isolates AVAILABILITY, not speed.
With --peer-mbps N, aggregate peer upload becomes a shared pool: each viewer
draws a pro-rata share of it, and the shortfall comes from origin.

Webseed: --webseed-mbps N adds a permanent full-file seed with N Mbps upload.
--compare-webseed runs every selected mode twice (webseed off / on) so the
collapse-cost difference is printed side by side.

Cost basis (matches frontend/app/demo/page.tsx): origin egress $0.045/GB,
same rate for the CloudFront comparison; P2P bytes free.

Usage:
    python3 backend/scripts/swarm_collapse_sim.py --mode steady --viewers 5000
    python3 backend/scripts/swarm_collapse_sim.py --mode collapse --viewers 10000 --watch-pct 80
    python3 backend/scripts/swarm_collapse_sim.py --mode all --seed 42
    python3 backend/scripts/swarm_collapse_sim.py --mode all --compare-webseed --webseed-mbps 200
"""

import argparse
import random
import sys

MB = 1024 * 1024
GB = 1024 * 1024 * 1024

# Matches the demo page's published basis.
ORIGIN_RATE_PER_GB = 0.045


class Viewer:
    __slots__ = ("arrive", "leave", "progress", "done")

    def __init__(self, arrive: float, leave: float):
        self.arrive = arrive
        self.leave = leave
        self.progress = 0.0  # bytes downloaded so far (prefix [0, progress])
        self.done = False  # has the full file; seeds it until leave


def schedule_viewers(mode: str, viewers: int, window: float, session_mean: float,
                     watch_pct: float, rng: random.Random,
                     file_bytes: float) -> list[Viewer]:
    """Return the viewer schedule for the scenario.

    ``window`` is the arrival window in seconds. ``session_mean`` is the mean
    session length in seconds. ``watch_pct`` is how far through the file the
    average viewer gets (0-100) — used to build the collapse/tail-gap waves.
    """
    out: list[Viewer] = []
    if mode in ("steady", "tail-gap") and mode == "steady":
        for _ in range(viewers):
            arrive = rng.uniform(0, window)
            session = max(1.0, rng.expovariate(1.0 / session_mean))
            out.append(Viewer(arrive, arrive + session))
    elif mode == "cold":
        # Short sessions, long gaps: sessions are sized so the swarm rarely
        # holds two people at once (mean session << mean inter-arrival gap),
        # so most bytes fall back to origin.
        gap = window / max(1, viewers)
        cold_session = max(1.0, gap / 3.0)
        for _ in range(viewers):
            arrive = rng.uniform(0, window)
            session = max(1.0, rng.expovariate(1.0 / cold_session))
            out.append(Viewer(arrive, arrive + session))
    elif mode in ("collapse", "tail-gap"):
        # Collapse splits the audience into two equal waves; tail-gap's single
        # burst IS the whole audience (its docstring says so — scheduling half
        # of it was a copy-paste artifact from the collapse waves).
        burst = viewers if mode == "tail-gap" else max(1, viewers // 2)
        # Wave 1: everyone watches to watch_pct, then leaves inside a short
        # window (the "event ends" moment). Session = time to download
        # watch_pct at a nominal stream rate + a jittered tail.
        # Nominal stream rate used ONLY to size the session: the sim drives
        # actual progress; leave time just must be >= arrive.
        nominal_rate = 2.0 * MB  # 2 MB/s ≈ 16 Mbps stream
        watch_bytes = file_bytes * (watch_pct / 100.0)
        for _ in range(burst):
            arrive = rng.uniform(0, window)
            session = watch_bytes / nominal_rate
            # Everyone departs in a ~1-minute window after finishing their watch.
            leave = arrive + session + rng.uniform(0, 60)
            out.append(Viewer(arrive, leave))
        if mode == "collapse":
            # Wave 2 lands AFTER wave 1 has fully left: the swarm is empty when
            # they arrive. With a webseed this is survivable; without one it is
            # the full-fallback scenario.
            wave2_start = window + (watch_bytes / nominal_rate) + 90
            for _ in range(viewers - burst):
                arrive = wave2_start + rng.uniform(0, window * 0.25)
                session = max(1.0, rng.expovariate(1.0 / session_mean))
                out.append(Viewer(arrive, arrive + session))
        # tail-gap: no wave 2 — the burst is the whole audience, and the
        # origin cost comes from the leading-edge collapse mid-stream.
    else:  # pragma: no cover
        raise ValueError(f"unknown mode {mode}")
    return out


def simulate(viewers: list[Viewer], file_bytes: float, tick: float,
             client_mbps: float, peer_mbps: float | None,
             unlimited_p2p: bool, webseed_mbps: float,
             rng: random.Random) -> dict:
    """Run the tick-based swarm and return origin/p2p accounting.

    Origin egress is split into three components so the webseed's effect is
    visible:

      stall_bytes  — served because NO peer was ahead (the 5s-fallback path).
                     Only possible when the webseed is off: with one, there is
                     always a peer ahead of every viewer.
      gap_bytes    — served because the aggregate peer pool couldn't supply
                     the demand fast enough (bandwidth shortfall). This is the
                     MINIMUM origin egress of the design.
      webseed_bytes— served by the webseed (also origin egress, but only the
                     gap: never a whole file per viewer).

    With the webseed on, stall_bytes is always 0 and origin = gap bytes
    (served via the webseed's bandwidth). If the webseed's bandwidth is
    under-provisioned, viewers are throttled (they progress slower than their
    client cap) rather than the origin serving beyond its means.
    """
    if not viewers:
        return {"origin_gb": 0.0, "p2p_gb": 0.0, "demand_gb": 0.0,
                "offload_pct": 0.0, "viewers": 0, "max_concurrency": 0,
                "stall_gb": 0.0, "gap_gb": 0.0, "webseed_gb": 0.0}
    client_cap = client_mbps * MB / 8.0 * tick  # bytes per tick
    peer_cap = None if unlimited_p2p else (peer_mbps or 5.0) * MB / 8.0 * tick
    webseed_cap = None if webseed_mbps <= 0 else webseed_mbps * MB / 8.0 * tick

    t_end = max(v.leave for v in viewers) + tick
    t = 0.0
    origin_bytes = 0.0
    p2p_bytes = 0.0
    stall_bytes = 0.0
    gap_bytes = 0.0
    webseed_bytes = 0.0
    max_concurrency = 0

    viewers_sorted = sorted(viewers, key=lambda v: v.arrive)
    vi = 0
    # Frontier = max progress among currently-active peers. Recomputed each
    # tick (leavers shrink it); gives an O(1) "anyone ahead?" check that is
    # byte-for-byte equivalent to the O(N) scan it replaces.
    max_progress = 0.0

    while t <= t_end:
        while vi < len(viewers_sorted) and viewers_sorted[vi].arrive <= t:
            vi += 1
        active = viewers_sorted[:vi]
        # Drop leavers: viewers whose session ended. They leave the swarm and
        # their bytes leave with them.
        active = [v for v in active if v.leave > t]
        max_concurrency = max(max_concurrency, len(active))
        max_progress = max((v.progress for v in active), default=0.0)

        need = [v for v in active if not v.done and v.progress < file_bytes]
        if need:
            if unlimited_p2p:
                # Availability-only: P2P is fast whenever a peer is ahead.
                # The running frontier starts at this tick's max and grows as
                # viewers below are processed, reproducing the original
                # order-dependent scan exactly.
                frontier = max_progress
                for v in need:
                    ahead = (webseed_cap is not None) or frontier > v.progress + 1
                    take = min(client_cap, file_bytes - v.progress)
                    if ahead:
                        p2p_bytes += take
                        v.progress += take
                    else:
                        stall_bytes += take
                        v.progress += take
                    if frontier < v.progress:
                        frontier = v.progress
            else:
                # Bounded aggregate peer upload: every active peer (seeder or
                # partial holder) contributes upload to a shared pool; viewers
                # draw a pro-rata share and the shortfall comes from origin.
                peer_pool = sum(peer_cap for w in active if w.progress > 0)
                demand = sum(min(client_cap, file_bytes - v.progress) for v in need)
                webseed_budget = webseed_cap  # per-tick shared origin bandwidth
                has_webseed = webseed_budget is not None
                frontier = max_progress  # grows as viewers below are served
                for v in need:
                    ahead = has_webseed or frontier > v.progress + 1
                    take = min(client_cap, file_bytes - v.progress)
                    if not ahead:
                        # Empty swarm, no webseed: full fallback, unbounded.
                        stall_bytes += take
                        v.progress += take
                        frontier = max(frontier, v.progress)
                        continue
                    # Peers first (free), then webseed covers the gap.
                    peer_share = min(take, peer_pool * (take / demand)) if demand > 0 else 0.0
                    p2p_bytes += peer_share
                    served = peer_share
                    if served < take and webseed_budget is not None and webseed_budget > 0:
                        ws = min(take - served, webseed_budget)
                        webseed_budget -= ws
                        webseed_bytes += ws
                        served += ws
                    if served < take:
                        if webseed_budget is not None:
                            # Under-provisioned origin: viewers are throttled
                            # rather than the origin serving beyond its means.
                            v.progress += served
                            frontier = max(frontier, v.progress)
                            continue
                        # No webseed: origin covers the shortfall directly.
                        gap_bytes += take - served
                        v.progress += take - served
                    else:
                        v.progress += served
                    frontier = max(frontier, v.progress)
        # Mark completions.
        for v in need:
            if v.progress >= file_bytes - 1:
                v.done = True

        t += tick

    origin_bytes = stall_bytes + gap_bytes + webseed_bytes
    demand_gb = (origin_bytes + p2p_bytes) / GB
    origin_gb = origin_bytes / GB
    p2p_gb = p2p_bytes / GB
    offload = (p2p_gb / demand_gb * 100.0) if demand_gb > 0 else 0.0
    return {
        "origin_gb": origin_gb,
        "p2p_gb": p2p_gb,
        "demand_gb": demand_gb,
        "offload_pct": offload,
        "viewers": len(viewers),
        "max_concurrency": max_concurrency,
        "stall_gb": stall_bytes / GB,
        "gap_gb": gap_bytes / GB,
        "webseed_gb": webseed_bytes / GB,
    }


def format_row(label: str, r: dict) -> str:
    cloudfront = r["demand_gb"] * ORIGIN_RATE_PER_GB
    torrentcdn = r["origin_gb"] * ORIGIN_RATE_PER_GB
    origin_pct = (100 - r["offload_pct"]) if r["demand_gb"] > 0 else 0.0
    return (
        f"{label:<10} viewers={r['viewers']:>7}  "
        f"peak={r['max_concurrency']:>6}  "
        f"delivered={r['demand_gb']:>9.1f} GB  "
        f"origin={r['origin_gb']:>9.1f} GB ({origin_pct:>5.1f}%)  "
        f"offload={r['offload_pct']:>5.1f}%  "
        f"CF=${cloudfront:>7.2f}  TC=${torrentcdn:>7.2f}"
    )


def run_scenario(mode: str, args, webseed_mbps: float | None = None) -> dict:
    rng = random.Random(args.seed)
    file_bytes = args.file_gb * GB
    viewers = schedule_viewers(
        mode, args.viewers, args.window, args.session_mean,
        args.watch_pct, rng, file_bytes,
    )
    tick = args.tick or (1.0 if args.viewers <= 20000 else 2.0)
    ws = args.webseed_mbps if webseed_mbps is None else webseed_mbps
    return simulate(viewers, file_bytes, tick, args.client_mbps,
                    args.peer_mbps, not args.no_unlimited_p2p, ws, rng)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["steady", "collapse", "tail-gap", "cold", "all"],
                   default="all", help="scenario to run (default: all)")
    p.add_argument("--viewers", type=int, default=5000, help="total viewers (default 5000)")
    p.add_argument("--file-gb", type=float, default=1.0, help="file size in GB (default 1.0)")
    p.add_argument("--window", type=float, default=1800.0,
                   help="arrival window in seconds (default 1800 = 30 min)")
    p.add_argument("--session-mean", type=float, default=600.0,
                   help="mean session length in seconds, steady/cold/wave2 (default 600)")
    p.add_argument("--watch-pct", type=float, default=80.0,
                   help="%% watched before leaving, collapse/tail-gap waves (default 80)")
    p.add_argument("--client-mbps", type=float, default=25.0,
                   help="per-viewer download cap in Mbps (default 25)")
    p.add_argument("--peer-mbps", type=float, default=None,
                   help="per-peer upload cap in Mbps; with --no-unlimited-p2p (default 5)")
    p.add_argument("--no-unlimited-p2p", action="store_true",
                   help="bound P2P by aggregate peer upload instead of assuming fast P2P")
    p.add_argument("--webseed-mbps", type=float, default=0.0,
                   help="webseed upload bandwidth in Mbps (0 = off, default 0)")
    p.add_argument("--compare-webseed", action="store_true",
                   help="run every selected mode twice (webseed off/on) side by side")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--tick", type=float, default=None,
                   help="seconds per simulation tick (default: auto — 1s at "
                        "≤20k viewers, else 2s). Larger ticks run "
                        "proportionally faster with coarser time steps.")
    args = p.parse_args()

    unlimited = not args.no_unlimited_p2p
    modes = ["steady", "tail-gap", "collapse", "cold"] if args.mode == "all" else [args.mode]
    print(f"file={args.file_gb} GB, origin rate=${ORIGIN_RATE_PER_GB}/GB, "
          f"client cap={args.client_mbps} Mbps, "
          f"P2P={'availability-only' if unlimited else f'{args.peer_mbps or 5} Mbps/peer bounded'}")
    print()
    for m in modes:
        if args.compare_webseed:
            off = run_scenario(m, args, webseed_mbps=0.0)
            on = run_scenario(m, args, webseed_mbps=args.webseed_mbps or 200.0)
            print(format_row(f"{m} (no ws)", off))
            print(format_row(f"{m} (+ws)", on))
            if off["demand_gb"] > 0:
                saved = off["origin_gb"] - on["origin_gb"]
                print(f"{'':<10} webseed saves {saved:>9.1f} GB origin "
                      f"(${saved * ORIGIN_RATE_PER_GB:>7.2f}); "
                      f"stall→gap: {off['stall_gb']:>7.1f}→{on['stall_gb']:>7.1f} GB")
            print()
        else:
            r = run_scenario(m, args)
            print(format_row(m, r))
            if r["demand_gb"] > 0 and (r["stall_gb"] or r["gap_gb"] or r["webseed_gb"]):
                print(f"{'':<10} origin split: stall={r['stall_gb']:>8.1f} GB  "
                      f"gap={r['gap_gb']:>8.1f} GB  webseed={r['webseed_gb']:>8.1f} GB")
            print()
    print("Legend: delivered = total bytes viewed; origin = bytes billed (left the server);")
    print("offload = P2P share; CF = same demand on CloudFront at $0.045/GB;")
    print("TC = TorrentCDN cost (origin bytes only); stall = origin served because")
    print("no peer was ahead (fallback path); gap = origin served due to peer")
    print("bandwidth shortfall; webseed = origin served via the webseed (gap only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())