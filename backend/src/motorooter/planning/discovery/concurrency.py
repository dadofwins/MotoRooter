"""How much metered work discovery runs at once.

Discovery is almost entirely waiting on four different APIs, so doing it one request at a
time is the wrong shape: a 300 km corridor took minutes of wall clock while the CPU did
nothing. It also cannot be unbounded — every provider here has a per-minute ceiling, and the
failure mode of exceeding one is a wave of 429s that looks exactly like the service being
broken.

One number, shared, because the ceiling being respected belongs to the stack rather than to
any single stage. The stages bound themselves with a plain `asyncio.Semaphore`: the pipeline
needs to emit several progress events per unit of work and the resolver needs input order
back, and no one helper is a good fit for both.
"""

import asyncio
from collections.abc import Coroutine, Sequence
from typing import Any

DEFAULT_CONCURRENCY = 6
"""Requests in flight per stage.

Chosen against the tightest per-minute ceiling in the stack rather than against what the
event loop could manage. Six concurrent searches at roughly a second each is around 360 a
minute at full tilt — comfortably inside Brave's and Places' limits with room for the retry
traffic a transient failure produces, and far enough below them that a burst does not turn
into a wave of 429s indistinguishable from an outage.

Per stage, not per run, so two stages overlapping can put twice this in flight. That is
accounted for in the headroom above rather than coordinated, since a global budget would
couple stages that otherwise know nothing about each other.
"""


async def bounded_gather[T](
    tasks: Sequence[Coroutine[Any, Any, T]], limit: int = DEFAULT_CONCURRENCY
) -> list[T | BaseException]:
    """Run `tasks` concurrently, at most `limit` at once, settling rather than raising.

    Here rather than in either caller because two stages need it and a copy in each is the
    drift this codebase keeps paying for.

    Batching a stage removes one unbounded call and puts unbounded *calls* in its place: a
    corridor with forty batches makes forty requests at once, which is growth in corridor
    length all over again. Both stages batched on 2026-08-26 shipped that way, and the
    ceiling above says exactly why it matters — exceeding a provider's per-minute limit
    produces a wave of 429s indistinguishable from an outage.

    Settles rather than raises, because every caller here degrades on a partial failure and
    needs to see which task failed rather than only the first exception.
    """
    gate = asyncio.Semaphore(max(limit, 1))

    async def run(task: Coroutine[Any, Any, T]) -> T:
        async with gate:
            return await task

    return list(await asyncio.gather(*(run(task) for task in tasks), return_exceptions=True))
