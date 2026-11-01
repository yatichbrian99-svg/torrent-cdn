"""The background-loop supervisor: restart, capped backoff, clean stop.

These tests pin the three transitions the checklist asks for (crash → restart,
backoff grows, clean stop) plus the two ways a supervisor loop usually goes
wrong: swallowing cancellation on shutdown, and restarting a factory that
returned on purpose (which turns a finished loop into a spin loop).
"""

from __future__ import annotations

import asyncio

import pytest

from app.ai.supervision import supervise


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def _noop_sleep(delay: float) -> None:
    return None


class TestSupervisor:
    @pytest.mark.asyncio
    async def test_a_crash_restarts_the_loop(self):
        calls: list[int] = []
        crashes: list[tuple[str, int, float]] = []

        def factory():
            async def run() -> None:
                calls.append(len(calls))
                if len(calls) < 3:
                    raise RuntimeError("coordinator unreachable")

            return run()

        await supervise(
            factory,
            should_stop=lambda: len(calls) >= 3,
            sleep=_noop_sleep,
            on_crash=lambda exc, failures, delay: crashes.append(
                (type(exc).__name__, failures, delay)
            ),
        )

        assert len(calls) == 3, "the loop was not restarted after the crash"
        assert [failure for _, failure, _ in crashes] == [1, 2]
        assert all(name == "RuntimeError" for name, _, _ in crashes)

    @pytest.mark.asyncio
    async def test_the_backoff_doubles_and_is_capped(self):
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        def factory():
            async def run() -> None:
                raise RuntimeError("still down")

            return run()

        await supervise(
            factory,
            base_delay=1.0,
            max_delay=8.0,
            should_stop=lambda: len(delays) >= 5,
            sleep=record_sleep,
        )

        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0], "backoff is not capped exponential"

    @pytest.mark.asyncio
    async def test_a_healthy_run_resets_the_backoff(self):
        """One blip after a long healthy run must not cost the capped delay."""
        clock = _Clock()
        delays: list[float] = []
        calls: list[int] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        def factory():
            async def run() -> None:
                calls.append(len(calls))
                if len(calls) == 1:
                    raise RuntimeError("blip on a streak of failures")
                if len(calls) == 2:
                    # This run stays healthy for well over `healthy_after`.
                    clock.now += 300.0
                    raise RuntimeError("crash after hours of good beats")

            return run()

        await supervise(
            factory,
            base_delay=1.0,
            max_delay=60.0,
            healthy_after=60.0,
            should_stop=lambda: len(delays) >= 2,
            sleep=record_sleep,
            clock=clock,
        )

        assert delays == [1.0, 1.0], (
            "the streak before the healthy run was not forgotten"
        )

    @pytest.mark.asyncio
    async def test_a_clean_stop_does_not_restart(self):
        stops = {"n": 0}
        crashes: list[BaseException] = []
        calls: list[int] = []

        def should_stop() -> bool:
            stops["n"] += 1
            return stops["n"] > 1

        def factory():
            async def run() -> None:
                calls.append(len(calls))
                raise RuntimeError("crashed just as we stopped")

            return run()

        await supervise(
            factory,
            should_stop=should_stop,
            sleep=_noop_sleep,
            on_crash=lambda exc, failures, delay: crashes.append(exc),
        )

        assert len(calls) == 1, "a stopped supervisor restarted the loop"
        assert len(crashes) == 1, "the crash that raced the stop was not reported"

    @pytest.mark.asyncio
    async def test_a_factory_that_returns_is_not_restarted(self):
        calls: list[int] = []

        def factory():
            async def run() -> None:
                calls.append(len(calls))

            return run()

        await supervise(factory, sleep=_noop_sleep)
        assert len(calls) == 1, "a finished loop was restarted"

    @pytest.mark.asyncio
    async def test_cancellation_propagates_instead_of_restarting(self):
        """Shutdown must not resurrect the loop it just cancelled."""
        calls: list[int] = []

        def factory():
            async def run() -> None:
                calls.append(len(calls))
                raise asyncio.CancelledError

            return run()

        with pytest.raises(asyncio.CancelledError):
            await supervise(factory, sleep=_noop_sleep)
        assert len(calls) == 1
