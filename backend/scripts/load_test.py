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
from concurrent.futures import ThreadPoolExecutor

import httpx

DEFAULT_FILE_ID = "4a130574-822d-43cb-a54c-f7db8204033d"  # live demo upload
ENDPOINTS = ["/files/public/{id}", "/analytics/public/{id}"]

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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="https://torrent-cdn.onrender.com")
    ap.add_argument("--file-id", default=DEFAULT_FILE_ID)
    ap.add_argument("--people", type=int, nargs="+", default=[5, 10, 25, 50, 100, 200],
                    help="concurrency levels to test (default: escalating ramp)")
    ap.add_argument("--duration", type=float, default=10.0, help="seconds per wave")
    ap.add_argument("--max-error-pct", type=float, default=5.0,
                    help="stop escalating if a wave exceeds this error %")
    ap.add_argument("--pause", type=float, default=5.0, help="seconds between waves")
    args = ap.parse_args()

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
