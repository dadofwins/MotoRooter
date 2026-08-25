"""The one interface every search source implements.

Narrow, like `RoutingProvider` and `LlmClient`, and for the same reason: a one-method protocol
makes a fake trivial, which is what keeps the pipeline testable without a search index.

Implementations must raise only `DiscoveryError` subclasses, so callers never catch something
vendor-shaped, and must never return a candidate that looks verified — verification is Places'
job and it has not run yet.
"""

from typing import Protocol, runtime_checkable

from motorooter.planning.discovery.models import Candidate
from motorooter.planning.discovery.queries import SearchQuery
from motorooter.routing.models import Coordinate

DEFAULT_RESULT_LIMIT = 5
"""Candidates per query. Each one costs a Places lookup downstream, so the search stage
bounds the fan-out rather than letting the next stage discover it."""


@runtime_checkable
class SearchSource(Protocol):
    """A source of candidate places along a corridor."""

    @property
    def name(self) -> str:
        """Recorded on every candidate as provenance."""
        ...

    async def search(
        self,
        query: SearchQuery,
        *,
        near: Coordinate,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> tuple[Candidate, ...]:
        """Candidates for one query.

        Args:
            query: what to search for, carrying its category and place.
            near: the corridor anchor. Recorded on each candidate as the only trustworthy
                location it has — a source's own claim about where something is may be wrong
                by a valley.
            limit: maximum candidates to return.

        Raises:
            DiscoveryUnavailable: transient upstream failure.
            DiscoveryQuotaExceeded: the search budget is spent.
            DiscoveryRateLimited: too many too quickly; retryable.
        """
        ...
