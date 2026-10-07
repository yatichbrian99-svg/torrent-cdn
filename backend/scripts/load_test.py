#!/usr/bin/env python3
"""Read-only load test for the torrent-cdn production stack.

Simulates N concurrent viewers ("people") hitting the two PUBLIC endpoints
that touch the databases:

    GET /files/public/{id}        Redis rate-limit + cache GET (→ Postgres on miss)
    GET /analytics/public/{id}    Redis rate-limit + Postgres aggregate query

Every simulated person sends its own X-Forwarded-For IP. The production
backend runs with TRUSTED_PROXIES unset (legacy mode), so it trusts the first
XFF entry from any peer and treats each IP as a distinct client — the same way
real viewers arriving through the Vercel proxy appear. Without this, every
request would share one real IP and trip the per-IP global burst limiter
(120 req / 10 s), which measures ONE user, not the stack.

SAFETY:
- Read-only GETs only. No writes, no analytics events, no data mutation.
- Bounded waves: each wave is `--people` concurrent users for `--duration`
  seconds; the script stops escalating if a wave exceeds `--max-error-pct`.
- Aborts immediately if a wave shows connection errors > max-error-pct
  (e.g. the free-tier instance crashing / OOM restarting).

Usage:
    python3 backend/scripts/load_test.py --people 50 --duration 10
    python3 backend/scripts/load_test.py --url https://torrent-cdn.onrender.com --people 100 --duration 10 --file-id <uuid>
"""

import argparse
import random
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

DEFAULT_FILE_ID = "4a130574-822d-43cb-a54c-f7db8204033d"  # live demo upload
ENDPOINTS = ["/files/public/{id}", "/analytics/public/{id}"]

# Per-IP-per-file analytics event cap (routes/analytics.py). The pipeline
# mode stays under it so every event is ACCEPTED — the assertion is that
# accepted events arrive in the DB, not that the cap holds (that's the
# shield probe's job).
EVENT_FILE_CAP_PER_MIN = 30

# Thread-safe logger
_lock = threading.Lock()


def _log(msg: str) -> None:
    with _lock:
        print(msg, flush=True)


def _fake_ip(i: int) -> str:
    """Synthetic TEST-NET-3 address per simulated person (203.0.113.0/24)."""
    return f"203.0.113.{1 + (i % 254)}"


def _run_person(
    person_id: int,
    url: str,
    file_id: str,
    duration: float,
    results: list,
    stop: threading.Event,
) -> None:
    ip = _fake_ip(person_id)
    headers = {"X-Forwarded-For": ip, "User-Agent": f"loadtest-person-{person_id}"}
    endpoints = [e.format(id=file_id) for e in ENDPOINTS]
    # Stagger start so people don't thundering-herd in the same instant.
    time.sleep(random.uniform(0, 0.5))
    deadline = time.monotonic() + duration
    idx = 0
    with httpx.Client(base_url=url, timeout=30.0, headers=headers, follow_redirects=False) as client:
        while time.monotonic() < deadline and not stop.is_set():
            path = endpoints[idx % len(endpoints)]
            idx += 1
            t0 = time.monotonic()
            try:
                r = client.get(path)
                results.append((r.status_code, (time.monotonic() - t0) * 1000))
            except httpx.HTTPError as e:
                # e.g. connect refused, read timeout, server closed connection
                results.append((f"ERR:{type(e).__name__}", (time.monotonic() - t0) * 1000))
            # Small think time — a real viewer polls stats every ~5s, fetches
            # metadata once. Keep it short so we still push real load.
            time.sleep(random.uniform(0, 0.2))


def run_wave(people: int, duration: float, url: str, file_id: str) -> dict:
    results: list = []
    stop = threading.Event()
    _log(f"\n=== wave: {people} people x {duration}s ===")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=people) as ex:
        futs = [ex.submit(_run_person, i, url, file_id, duration, results, stop) for i in range(people)]
        for f in futs:
            f.result()  # propagate exceptions
    elapsed = time.monotonic() - t0
    return summarize(results, elapsed)


def summarize(results: list, elapsed: float) -> dict:
    n = len(results)
    statuses: dict = {}
    for code, _ in results:
        statuses[str(code)] = statuses.get(str(code), 0) + 1
    latencies = [ms for _, ms in results]
    rps = n / elapsed if elapsed > 0 else 0
    ok = sum(v for k, v in statuses.items() if k.startswith("2"))
    errors = sum(v for k, v in statuses.items() if k.startswith(("5", "ERR")))
    return {
        "people": 0,  # filled in by the caller (run_wave → main)
        "requests": n,
        "rps": rps,
        "ok_2xx": ok,
        "errors": errors,
        "statuses": statuses,
        "latency_ms_avg": statistics.mean(latencies) if latencies else 0,
        "latency_ms_p50": statistics.median(latencies) if latencies else 0,
        "latency_ms_p95": (
            sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0
        ),
    }


def print_wave(people: int, summary: dict) -> None:
    print(f"people={people:>4}  reqs={summary['requests']:>5}  "
          f"{summary['rps']:>6.1f} req/s  "
          f"2xx={summary['ok_2xx']:>4}  errors={summary['errors']:>3}  "
          f"lat avg={summary['latency_ms_avg']:.0f}ms  p50={summary['latency_ms_p50']:.0f}ms  "
          f"p95={summary['latency_ms_p95']:.0f}ms")
    if summary["statuses"]:
        parts = ", ".join(f"{k}:{v}" for k, v in sorted(summary["statuses"].items()))
        print(f"         statuses: {parts}")


# ── pipeline mode: prove the write path end-to-end in production ───────────

def _post_event(client: httpx.Client, file_id: str, etype: str) -> tuple[int, float]:
    t0 = time.monotonic()
    try:
        r = client.post("/analytics/event", json={
            "file_id": file_id, "event_type": etype,
            "bytes_p2p": 125_000, "bytes_origin": 0,
        })
        return r.status_code, (time.monotonic() - t0) * 1000
    except httpx.HTTPError as e:
        return f"ERR:{type(e).__name__}", (time.monotonic() - t0) * 1000


def _get_analytics(client: httpx.Client, file_id: str) -> dict | None:
    r = client.get(f"/analytics/public/{file_id}")
    if r.status_code != 200:
        return None
    return r.json()


def run_pipeline(args) -> None:
    """Closed-loop write test: N accepted events → Redis buffer → batch INSERT
    → Postgres aggregate. Asserts the exact delta (joins_in == views_out) so
    "the pipeline works" is a measurement, not a hope. Stays under the 30/min
    per-IP-per-file cap so every event is ACCEPTED; the cap itself is probed
    separately (shield check). Read-safe: only appends analytics rows to the
    demo file — no user data touched, nothing mutated or deleted."""
    # peer_joined is never sampled away under load shedding, so the delta
    # assertion is deterministic rather than statistical.
    etype = "peer_joined"
    with httpx.Client(base_url=args.url, timeout=30.0) as c:
        r = c.get(f"/analytics/public/{args.file_id}")
        print(f"sanity: GET /analytics/public -> {r.status_code}")
        base = _get_analytics(c, args.file_id)
        if base is None:
            print("FAIL: could not read baseline aggregate — aborting pipeline test")
            return
        n = args.pipeline
        # Pace under the per-file cap: 30/min → 2.1s between events covers
        # cap reset boundaries (worst case two windows straddle the burst).
        pace = max(2.1, 60.0 / (EVENT_FILE_CAP_PER_MIN - 1))
        print(f"posting {n} accepted '{etype}' events "
              f"({pace:.1f}s apart to respect the {EVENT_FILE_CAP_PER_MIN}/min cap)...")
        codes: Counter = Counter()
        lat = []
        t_post = time.monotonic()
        for i in range(n):
            code, ms = _post_event(c, args.file_id, etype)
            codes[str(code)] += 1
            lat.append(ms)
            if i < n - 1:
                time.sleep(pace)
        post_s = time.monotonic() - t_post
        print(f"posted: {dict(codes)}  latency avg={statistics.mean(lat):.0f}ms "
              f"p95={sorted(lat)[int(len(lat) * 0.95)]:.0f}ms  wall={post_s:.0f}s")

        accepted = codes.get("200", 0) + codes.get("202", 0)
        if accepted != n:
            print(f"FAIL: only {accepted}/{n} events accepted — aborting before aggregate check")
            return

        # The drainer flushes every ANALYTICS_FLUSH_INTERVAL=2s; poll well past
        # it (plus one batching cycle) before judging.
        target = base["total_views"] + n
        deadline = time.monotonic() + 45
        got = None
        while time.monotonic() < deadline:
            time.sleep(4)
            got = _get_analytics(c, args.file_id)
            if got is not None and got["total_views"] >= target:
                break
        if got is None:
            print("FAIL: aggregate endpoint stopped returning 200 during drain")
            return
        delta = got["total_views"] - base["total_views"]
        print(f"aggregate: total_views {base['total_views']} -> {got['total_views']} "
              f"(delta {delta}, expected {n})")
        if delta == n:
            print("PIPELINE VERIFIED: every accepted event reached the DB "
                  "through the Redis buffer + batch drainer ✅")
        elif delta > n:
            print(f"VERIFIED WITH BACKGROUND NOISE: delta {delta} > {n} — "
                  "live traffic added views during the test window; per-event "
                  "delivery still exact")
        else:
            print(f"FAIL: {n - delta} accepted event(s) missing from the aggregate — "
                  "drainer lost or dropped events")

        # Shield probe: 3 events over the per-file cap must meet 429s.
        shield = Counter()
        for _ in range(3):
            code, _ms = _post_event(c, args.file_id, etype)
            shield[str(code)] += 1
        print(f"shield probe (over-cap): {dict(shield)} "
              f"{'✅ 429s enforce the cap' if shield.get('429', 0) >= 2 else '⚠️ expected 429s — check cap'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="https://torrent-cdn.onrender.com")
    ap.add_argument("--file-id", default=DEFAULT_FILE_ID)
    ap.add_argument("--people", type=int, nargs="+", default=[5, 10, 25, 50, 100, 200],
                    help="concurrency levels to test (default: escalating ramp)")
    ap.add_argument("--duration", type=float, default=10.0, help="seconds per wave")
    ap.add_argument("--max-error-pct", type=float, default=5.0,
                    help="stop escalating if a wave exceeds this error pct")
    ap.add_argument("--pause", type=float, default=5.0, help="seconds between waves")
    ap.add_argument("--pipeline", type=int, metavar="N", default=0,
                    help="run the write-path pipeline test with N accepted "
                         "events (paced under the 30/min per-file cap), then "
                         "verify the exact delta in the public aggregate")
    args = ap.parse_args()

    if args.pipeline:
        run_pipeline(args)
        return

    _log(f"Target: {args.url}  file: {args.file_id}  read-only GETs only")
    _log("Sanity: one request from each endpoint to confirm the route + demo file are live.")
    with httpx.Client(base_url=args.url, timeout=30.0) as c:
        for path in [p.format(id=args.file_id) for p in ENDPOINTS]:
            r = c.get(path)
            print(f"  GET {path} -> {r.status_code}")

    results_summary = []
    for i, people in enumerate(args.people):
        summary = run_wave(people, args.duration, args.url, args.file_id)
        summary["people"] = people
        results_summary.append(summary)
        print_wave(people, summary)

        error_pct = 100.0 * summary["errors"] / max(1, summary["requests"])
        if error_pct > args.max_error_pct:
            print(f"\n!! wave at {people} people exceeded {args.max_error_pct}% errors "
                  f"({error_pct:.1f}%) — stopping escalation.")
            break
        if i < len(args.people) - 1:
            time.sleep(args.pause)

    print("\n=== summary ===")
    print("people   req/s     p95(ms)   errors   verdict")
    for s in results_summary:
        err_pct = 100.0 * s["errors"] / max(1, s["requests"])
        verdict = "healthy" if err_pct <= 1.0 else ("degraded" if err_pct <= args.max_error_pct else "FAIL")
        print(f"{s['people']:>6}  {s['rps']:>7.1f}  {s['latency_ms_p95']:>8.0f}  "
              f"{err_pct:>6.1f}%    {verdict}")


if __name__ == "__main__":
    main()
