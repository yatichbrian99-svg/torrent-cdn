"""Supervise a long-lived background coroutine.

A bare ``asyncio.create_task`` is fire-and-forget: when the coroutine raises,
nothing collects the exception, the task dies, and the work it was doing simply
stops. That is how a worker could keep answering HTTP while its heartbeat was
dead — the coordinator's liveness gate then quietly stopped routing to it, and
the only trace was a line on stderr that nobody read. The same shape appears
wherever a worker or coordinator starts a "run forever" loop.

:func:`supervise` runs such a factory in a loop and makes three things explicit:

* **a crash is restarted** with capped exponential backoff, instead of the loop
  silently disappearing;
* **the backoff grows** ``base_delay`` → ``max_delay`` across consecutive
  failures, so a coordinator that is down is not hammered every 30 s, and
  **resets** after a run that stayed healthy for ``healthy_after`` seconds, so
  one blip after hours of good beats does not cost a minute of silence;
* **a clean stop ends it** — ``should_stop`` is checked before every restart, and
  a factory that returns on its own is treated as finished rather than restarted
  (restarting it would spin on whatever made it return).

``asyncio.CancelledError`` is deliberately not caught, so cancelling the task
during shutdown stops the supervisor instead of resurrecting the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


def _never_stop() -> bool:
    return False


async def supervise(
    factory: Callable[[], Awaitable[None]],
    *,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    healthy_after: float = 60.0,
    should_stop: Callable[[], bool] = _never_stop,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_crash: Callable[[BaseException, int, float], None] | None = None,
) -> None:
    """Run ``factory`` until it returns cleanly or ``should_stop()`` is true.

    ``on_crash(exc, consecutive_failures, delay_before_restart)`` is called for
    every crash, before the backoff sleep, so the caller can log or alert without
    this module knowing how. ``sleep`` and ``clock`` are seams for tests.
    """
    delay = base_delay
    failures = 0
    while not should_stop():
        started = clock()
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ran_for = clock() - started
            if ran_for >= healthy_after:
                # Long healthy run: this crash is a new incident, not a streak.
                failures = 0
                delay = base_delay
            failures += 1
            if on_crash is not None:
                on_crash(exc, failures, delay)
            if should_stop():
                break
            await sleep(delay)
            delay = min(delay * 2, max_delay)
            continue
        # Returned on its own: finished, not crashed.
        logger.debug("supervised task returned cleanly; not restarting")
        return
