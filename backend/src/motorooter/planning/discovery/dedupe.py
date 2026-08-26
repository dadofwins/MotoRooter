"""Not issuing the same search twice within one run.

Two corridors have shown the waste. Three anchors on Ellensburg-Cashmere all reverse-geocoded
to "Liberty"; two on Chinook Pass both to "Mather Memorial Parkway". On a corridor that is
one long road, every anchor gets the same name, and each duplicate is a metered request for
an answer already in hand.

**Deduplication, not caching, and the distinction is contractual.** Brave's terms permit
"transient storage required for operation of Customer Applications" and forbid everything
past it, so a result may not outlive the run that fetched it. This is therefore built per run
and discarded with it — which is also why it cannot sit in `build_discovery` beside the retry
decorator, constructed once and reused for the life of the process. A cache there is exactly
what the terms prohibit.

It is the only thing available that reduces request volume, since caching is not.
"""

import asyncio

from motorooter.planning.discovery.models import Candidate
from motorooter.planning.discovery.protocol import DEFAULT_RESULT_LIMIT, SearchSource
from motorooter.planning.discovery.queries import SearchQuery
from motorooter.routing.models import Coordinate

_Key = tuple[str, str]


class DeduplicatingSearchSource:
    """Collapses identical searches, for the lifetime of one run.

    Concurrent callers share one in-flight request rather than each starting their own: the
    anchors run in parallel, so the duplicates arrive simultaneously and a check-then-act
    would let all of them through. That is the case worth handling — the sequential one
    barely happens.
    """

    def __init__(self, inner: SearchSource) -> None:
        self._inner = inner
        self._in_flight: dict[_Key, asyncio.Task[tuple[Candidate, ...]]] = {}

    @property
    def name(self) -> str:
        """The wrapped source's name; candidates carry it as provenance."""
        return self._inner.name

    async def search(
        self,
        query: SearchQuery,
        *,
        near: Coordinate,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> tuple[Candidate, ...]:
        key = (query.text, query.category.value)
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._inner.search(query, near=near, limit=limit))
            self._in_flight[key] = task
        try:
            return _found_near(await asyncio.shield(task), near)
        except BaseException:
            # Failures are not remembered. Sharing a retryable error with every later caller
            # would turn one bad moment into a corridor-wide outage, and the retry layer
            # underneath would never get another attempt.
            #
            # Discarded by identity: a later caller may already have started a replacement,
            # and dropping theirs would leave the entry pointing at nothing.
            if self._in_flight.get(key) is task:
                del self._in_flight[key]
            raise


def _found_near(candidates: tuple[Candidate, ...], near: Coordinate) -> tuple[Candidate, ...]:
    """The shared results, re-stamped with this caller's own anchor.

    `found_near` is not decoration: it becomes the location bias for the Places lookup, and
    it is the only trustworthy position a candidate has before resolution. Handing the second
    caller the first one's anchor would quietly move its lookup by the distance between
    anchors, which on a real corridor is 25 km.

    So the *request* is shared and the *provenance* is not. That is what makes the anchor
    safe to leave out of the key — and leaving it out is what makes this work at all, since
    the duplicate names it exists for are 25 km apart by construction.
    """
    return tuple(candidate.model_copy(update={"found_near": near}) for candidate in candidates)
