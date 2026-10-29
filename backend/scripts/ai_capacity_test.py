#!/usr/bin/env python3
"""Capacity test for the TCDN-AI surface: ramp, sustain, 10x spike.

Drives the real HTTP path — ``POST /v1/chat/completions`` on the coordinator,
which places the job, calls the worker over the encrypted protocol, receives the
signed receipt and settles it — with **many API keys**, each on its own account
so the per-account concurrency cap is not the thing being measured.

Phases (row 10.2):

1. **ramp** — a fixed number of requests at each concurrency level, to find the
   highest level that stays under ``--max-error-pct``;
2. **sustain** — that level held for ``--sustain-seconds`` (default 30 min);
3. **spike** — ten times the sustain concurrency for ``--spike-seconds``.

Requests are read-mostly for the customer (a tiny completion) but each one
exercises auth, pricing, a hold, a signed receipt and a settlement, so the
numbers are the money path's, not a static endpoint's.

API keys live in a JSON file produced by the setup step; they are never written
to the report.

Usage::

    python scripts/ai_capacity_test.py --base-url http://127.0.0.1:8000 \
        --keys-file /tmp/capacity-keys.json --model capacity-model \
        --ramp 2,4,8,16,32 --ramp-seconds 20 \
        --sustain-seconds 1800 --spike-seconds 60 \
        --out ../docs/tcdn-ai/evidence/14-capacity/capacity.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from pathlib import Path

import httpx


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered)) - 1))
    return round(ordered[index], 1)


def _machine() -> dict:
    memory_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory_kb = int(line.split()[1])
                break
    except OSError:
        pass
    return {
        "cpu_count": os.cpu_count(),
        "memory_gb": round(memory_kb / 1024 / 1024, 1),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "load_average": [round(v, 2) for v in os.getloadavg()]
        if hasattr(os, "getloadavg")
        else [],
    }


class Stats:
    def __init__(self) -> None:
        self.latencies: list[float] = []
        self.statuses: dict[str, int] = {}
        self.started = time.monotonic()
        self.finished = 0.0

    def add(self, status: str, ms: float) -> None:
        self.statuses[status] = self.statuses.get(status, 0) + 1
        self.latencies.append(ms)

    def close(self) -> dict:
        self.finished = time.monotonic()
        elapsed = self.finished - self.started
        ok = sum(v for k, v in self.statuses.items() if k.startswith("2"))
        errors = sum(v for k, v in self.statuses.items() if k.startswith(("5", "ERR")))
        throttled = self.statuses.get("429", 0)
        return {
            "ok_rate_pct": round(ok / len(self.latencies) * 100, 2)
            if self.latencies
            else 0.0,
            "requests": len(self.latencies),
            "rps": round(len(self.latencies) / elapsed, 1) if elapsed > 0 else 0.0,
            "ok_2xx": ok,
            "errors_5xx_or_conn": errors,
            "throttled_429": throttled,
            "error_pct": round(errors / len(self.latencies) * 100, 2)
            if self.latencies
            else 0.0,
            "status_counts": dict(sorted(self.statuses.items())),
            "latency_ms": {
                "p50": _percentile(self.latencies, 50),
                "p95": _percentile(self.latencies, 95),
                "p99": _percentile(self.latencies, 99),
                "max": round(max(self.latencies), 1) if self.latencies else 0.0,
            },
            "elapsed_seconds": round(elapsed, 1),
        }


async def _worker(
    client: httpx.AsyncClient,
    key: str,
    ip: str,
    model: str,
    stop: asyncio.Event,
    stats: Stats,
    think: float,
) -> None:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "capacity probe"}],
        "max_tokens": 8,
    }
    # One synthetic client IP per key: the per-IP bucket measures ONE viewer, so
    # sharing 127.0.0.1 across every worker would measure the rate limiter (as
    # it should) rather than the deployment. TEST-NET-3, never routed.
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Forwarded-For": ip,
    }
    while not stop.is_set():
        started = time.monotonic()
        try:
            response = await client.post(
                "/v1/chat/completions", json=body, headers=headers
            )
            stats.add(str(response.status_code), (time.monotonic() - started) * 1000)
        except httpx.HTTPError as exc:
            stats.add(f"ERR:{type(exc).__name__}", (time.monotonic() - started) * 1000)
        if think:
            await asyncio.sleep(think)


async def run_phase(
    *,
    base_url: str,
    keys: list[str],
    model: str,
    concurrency: int,
    seconds: float,
    think: float = 0.0,
) -> dict:
    stop = asyncio.Event()
    stats = Stats()
    limits = httpx.Limits(
        max_connections=concurrency + 8, max_keepalive_connections=concurrency + 8
    )
    async with httpx.AsyncClient(
        base_url=base_url, timeout=60.0, limits=limits
    ) as client:
        tasks = [
            asyncio.create_task(
                _worker(
                    client,
                    keys[i % len(keys)],
                    f"203.0.113.{1 + (i % 250)}",
                    model,
                    stop,
                    stats,
                    think,
                )
            )
            for i in range(concurrency)
        ]
        await asyncio.sleep(seconds)
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {"concurrency": concurrency, **stats.close()}


def _parse_levels(raw: str) -> list[int]:
    levels = sorted({int(part) for part in raw.split(",") if part.strip()})
    if not levels or levels[0] < 1:
        raise SystemExit("--ramp must be a comma-separated list of positive integers")
    return levels


async def run(args: argparse.Namespace) -> dict:
    payload = json.loads(Path(args.keys_file).read_text())
    keys = [entry["key"] if isinstance(entry, dict) else entry for entry in payload]
    if len(keys) < args.ramp_max:
        print(
            f"note: {len(keys)} keys for concurrency {args.ramp_max}; "
            "keys are reused round-robin (each holds its own account)",
            file=sys.stderr,
        )

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        status = (await client.get("/api/ai/v1/status")).json()
    server = {
        "commit": status.get("commit"),
        "database": status.get("database", {}).get("status"),
        "redis": status.get("redis", {}).get("status"),
        "workers_registered": status.get("workers", {}).get("registered"),
        "workers_healthy": status.get("workers", {}).get("healthy"),
        "web_concurrency": os.environ.get("WEB_CONCURRENCY", "1"),
    }

    report: dict = {
        "base_url": args.base_url,
        "model": args.model,
        "keys": len(keys),
        "machine": _machine(),
        "server": server,
        "phases": {},
    }
    ramp: list[dict] = []
    best = 1
    for level in _parse_levels(args.ramp):
        result = await run_phase(
            base_url=args.base_url,
            keys=keys,
            model=args.model,
            concurrency=level,
            seconds=args.ramp_seconds,
            think=args.think,
        )
        ramp.append(result)
        print(
            f"ramp {level:>3} -> {result['rps']:>7} rps, p95 "
            f"{result['latency_ms']['p95']:>7} ms, 2xx {result['ok_rate_pct']}% "
            f"(429 {result['throttled_429']}, 5xx {result['errors_5xx_or_conn']})"
        )
        if (
            result["error_pct"] <= args.max_error_pct
            and result["ok_rate_pct"] >= args.min_ok_pct
        ):
            best = level
        else:
            break
    report["phases"]["ramp"] = ramp
    report["selected_concurrency"] = best

    sustain = await run_phase(
        base_url=args.base_url,
        keys=keys,
        model=args.model,
        concurrency=best,
        seconds=args.sustain_seconds,
        think=args.think,
    )
    report["phases"]["sustain"] = sustain
    print(
        f"sustain {best} for {args.sustain_seconds:.0f}s -> {sustain['rps']} rps, "
        f"p95 {sustain['latency_ms']['p95']} ms, 2xx {sustain['ok_rate_pct']}%, "
        f"5xx/conn {sustain['error_pct']}%"
    )

    spike_concurrency = best * args.spike_multiplier
    spike = await run_phase(
        base_url=args.base_url,
        keys=keys,
        model=args.model,
        concurrency=spike_concurrency,
        seconds=args.spike_seconds,
        think=0.0,
    )
    report["phases"]["spike"] = spike
    print(
        f"spike {spike_concurrency} -> {spike['rps']} rps, "
        f"p95 {spike['latency_ms']['p95']} ms, 2xx {spike['ok_rate_pct']}%, "
        f"429 {spike['throttled_429']}, 5xx/conn {spike['errors_5xx_or_conn']}"
    )

    # A spike is allowed to throttle (429 is the designed backpressure) but not
    # to fail requests or to return nothing at all.
    report["ok"] = (
        sustain["requests"] > 0
        and sustain["error_pct"] <= args.max_error_pct
        and sustain["ok_rate_pct"] >= args.min_ok_pct
        and spike["rps"] > 0
        and spike["errors_5xx_or_conn"] <= max(1, spike["requests"] * 0.01)
    )
    report["server_size_note"] = (
        "single uvicorn worker (WEB_CONCURRENCY=1) and one echo worker process on "
        "the same machine; the numbers are the whole request path, not a mock"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--keys-file", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--ramp", default="1,2,4,8,16")
    parser.add_argument("--ramp-seconds", type=float, default=20.0)
    parser.add_argument("--sustain-seconds", type=float, default=1800.0)
    parser.add_argument("--spike-seconds", type=float, default=60.0)
    parser.add_argument("--spike-multiplier", type=int, default=10)
    parser.add_argument("--max-error-pct", type=float, default=1.0)
    parser.add_argument(
        "--min-ok-pct",
        type=float,
        default=95.0,
        help="a ramp level is sustainable only if this share of requests is 2xx",
    )
    parser.add_argument(
        "--think",
        type=float,
        default=1.0,
        help="seconds a client waits between requests (keeps each key under its rate limit)",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    args.ramp_max = max(_parse_levels(args.ramp))

    report = asyncio.run(run(args))
    print(json.dumps({k: report[k] for k in ("selected_concurrency", "ok")}, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
