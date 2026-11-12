#!/usr/bin/env python3
"""Basic load test for the TCDN-AI coordinator.

Measures **placement** latency/error rate by default. `--ack` additionally runs a
full job would require a live worker, so it is intentionally not attempted here:
inference throughput must be measured on the real engine.

Usage:
    python scripts/ai_load_test.py --base-url http://127.0.0.1:8000 \
        --api-key "$TCDN_AI_API_KEY" --model llama-3.1-8b-instruct \
        --requests 200 --concurrency 20
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def one(
    client: httpx.AsyncClient, base: str, headers: dict, model: str
) -> tuple[float, int]:
    started = time.perf_counter()
    response = await client.post(
        f"{base}/api/ai/v1/placements",
        headers=headers,
        json={"model": model, "max_tokens": 16, "prompt_tokens_estimate": 8},
    )
    elapsed = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        try:
            code = response.json()["error"]["code"]
        except (ValueError, KeyError):
            code = response.status_code
        print(f"error: {response.status_code} {code}")
    return elapsed, response.status_code


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.api_key}"}
    semaphore = asyncio.Semaphore(args.concurrency)
    latencies: list[float] = []
    statuses: dict[int, int] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        async def guarded() -> None:
            async with semaphore:
                elapsed, status = await one(client, base, headers, args.model)
                latencies.append(elapsed)
                statuses[status] = statuses.get(status, 0) + 1

        started = time.perf_counter()
        await asyncio.gather(*(guarded() for _ in range(args.requests)))
        wall = time.perf_counter() - started

    latencies.sort()

    def pct(value: float) -> float:
        index = min(len(latencies) - 1, int(len(latencies) * value))
        return latencies[index]

    print(f"requests={len(latencies)} wall_s={wall:.2f} "
          f"rps={len(latencies) / wall:.1f}")
    print(f"placement p50={pct(0.50):.1f}ms p95={pct(0.95):.1f}ms "
          f"mean={statistics.fmean(latencies):.1f}ms")
    print(f"status_counts={statuses}")
    print("note: this measures placement only; inference throughput must be "
          "measured against the real engine")


if __name__ == "__main__":
    asyncio.run(main())
