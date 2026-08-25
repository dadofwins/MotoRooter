"""Time as a dependency.

Retry backoff and quota windows are both time-driven. Injecting the clock keeps their
tests instant and deterministic instead of slow and flaky.
"""

import asyncio
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float:
        """Seconds from an arbitrary epoch. Only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Real time. Monotonic, so it is immune to wall-clock adjustments."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Manually advanced clock. `sleep` jumps forward instead of waiting."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds
