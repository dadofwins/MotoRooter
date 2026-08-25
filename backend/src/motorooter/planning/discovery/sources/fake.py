"""Scripted search source.

Ships in `src` because it is a supported seam: the pipeline tests, `MOTOROOTER_OFFLINE=1`,
and local development without a Brave key all depend on it.

Returns candidates keyed by category so a pipeline test can assert on plausible-looking
results without a search index, and can be told to fail on demand — which a real search
engine will not do to order.
"""

from collections.abc import Sequence

from motorooter.planning.discovery.errors import DiscoveryError
from motorooter.planning.discovery.models import Candidate
from motorooter.planning.discovery.protocol import DEFAULT_RESULT_LIMIT
from motorooter.planning.discovery.queries import SearchQuery
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory

FAKE_SOURCE = "fake-search"


class FakeSearchSource:
    """Returns invented candidates, recording what it was asked."""

    def __init__(
        self,
        *,
        names: Sequence[str] | None = None,
        error: DiscoveryError | None = None,
        name: str = FAKE_SOURCE,
    ) -> None:
        """
        Args:
            names: candidate names to return for every query. Defaults to three.
            error: raised instead of searching, for failure-path tests.
            name: reported as the source, and recorded on each candidate.
        """
        self._names = list(names) if names is not None else ["First", "Second", "Third"]
        self._error = error
        self._name = name
        self.queries: list[SearchQuery] = []
        """Every query received, so a test can assert the fan-out rather than infer it."""

    @property
    def name(self) -> str:
        return self._name

    async def search(
        self,
        query: SearchQuery,
        *,
        near: Coordinate,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> tuple[Candidate, ...]:
        self.queries.append(query)
        if self._error is not None:
            raise self._error

        return tuple(
            Candidate(
                name=f"{candidate_name} near {query.place}",
                category=query.category,
                found_near=near,
                source=self._name,
                snippet=f"a plausible-sounding description of {candidate_name}",
                url=f"https://example.test/{candidate_name.lower()}",
            )
            for candidate_name in self._names[:limit]
        )


def category_of(query: SearchQuery) -> PoiCategory:
    """Convenience for tests that only care which category a query was for."""
    return query.category
