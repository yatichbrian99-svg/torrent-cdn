"""Read-only liveness status for a TCDN-AI deployment.

This is the "is it alive?" surface an operator opens first: which build is
running, whether the AI flag is on, whether Postgres and Redis answer, and what
the worker registry says (how many nodes, how fresh their last heartbeat is).

Deliberately public and deliberately boring:

* no API keys, no node ids, no endpoints, no key ids, no prompts — the same
  posture as ``/health`` and ``/api/health/live`` in the host app;
* counts and ages only, so the payload is useful to the owner and worthless to
  an attacker mapping the fleet;
* every probe is wrapped: a broken dependency is reported as a status string,
  never raised, because a status page that 500s when the database is down is the
  one time it would have been worth reading.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .service import HEARTBEAT_TIMEOUT_SECONDS, Coordinator

#: Short commit shown to the operator. Render injects RENDER_GIT_COMMIT on every
#: deploy; anywhere else falls back to GIT_COMMIT if a build baked one in.
_COMMIT_ENV = ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION")


def build_commit() -> str | None:
    for name in _COMMIT_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            # A full SHA is noise on a status page; 12 chars is unambiguous.
            return value[:12]
    return None


def _probe_result(started: float, error: str | None) -> dict[str, Any]:
    return {
        "status": "error" if error else "ok",
        "latency_ms": int((time.perf_counter() - started) * 1000),
        **({"detail": error} if error else {}),
    }


async def database_probe() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        from ..database import get_db_pool

        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:  # reported, never raised
        return _probe_result(started, type(exc).__name__)
    return _probe_result(started, None)


async def redis_probe() -> dict[str, Any]:
    """Redis is optional for the CDN, but *not* for a safe AI deployment.

    Without it the replay cache fails closed, so every ticket claim is refused
    and placement stops. That makes "absent" a distinct, actionable status here
    rather than the quiet degradation it is elsewhere in the app.
    """
    started = time.perf_counter()
    try:
        from ..database import get_redis

        redis = await get_redis()
        if redis is None:
            return {
                "status": "absent",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "detail": "REDIS_URL unset or unreachable — placement will fail closed",
            }
        await redis.ping()
    except Exception as exc:
        return _probe_result(started, type(exc).__name__)
    return _probe_result(started, None)


async def worker_summary(coordinator: Coordinator, *, now: int) -> dict[str, Any]:
    """Counts and heartbeat ages, never identities."""
    nodes = await coordinator.store.list_nodes()
    ages: list[int] = []
    healthy = 0
    models: set[str] = set()
    for node in nodes:
        if node.last_heartbeat is None:
            continue
        age = max(0, now - int(node.last_heartbeat))
        ages.append(age)
        if age <= HEARTBEAT_TIMEOUT_SECONDS and not node.suspended:
            healthy += 1
        models.update(node.models)
    return {
        "registered": len(nodes),
        "healthy": healthy,
        "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT_SECONDS,
        "newest_heartbeat_age_seconds": min(ages) if ages else None,
        "oldest_heartbeat_age_seconds": max(ages) if ages else None,
        "models_served": sorted(models),
    }


async def build_status(
    coordinator: Coordinator, *, now: int | None = None
) -> dict[str, Any]:
    now_ts = int(now if now is not None else time.time())
    settings = coordinator.settings
    try:
        workers = await worker_summary(coordinator, now=now_ts)
        workers_error = None
    except Exception as exc:
        workers = {"registered": None, "healthy": None}
        workers_error = type(exc).__name__

    return {
        "service": "tcdn-ai-coordinator",
        "commit": build_commit(),
        "environment": settings.environment,
        "feature_flag": {
            "name": "ENABLE_TCDN_AI",
            "on": os.environ.get("ENABLE_TCDN_AI", "").strip().lower()
            in {"1", "true", "yes"},
            "routes_mounted": True,
        },
        "statement": "Gateway mode: the coordinator sees prompts in transit and stores none.",
        "database": await database_probe(),
        "redis": await redis_probe(),
        "workers": workers,
        **({"workers_error": workers_error} if workers_error else {}),
        "server_time": now_ts,
    }


__all__ = ["build_commit", "build_status", "worker_summary"]
