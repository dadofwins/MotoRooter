"""Shared contract every `SearchSource` must satisfy.

Subclass and override the `source` fixture. Same purpose as the routing adapter contract: if
an adapter passes this, the pipeline can substitute it without callers noticing, and a
guarantee added here binds every present and future source at once.

The guarantees are mostly about what a source must *not* do. It returns claims, and the
pipeline's whole safety property rests on those claims never looking more trustworthy than
they are.
"""

import pytest

from motorooter.planning.discovery.protocol import SearchSource
from motorooter.planning.discovery.queries import queries_for
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory

ANCHOR = Coordinate(lat=47.0, lon=-121.0)


class SearchSourceContract:
    @pytest.fixture
    def source(self) -> SearchSource:
        raise NotImplementedError("override the `source` fixture")

    @pytest.fixture
    def query(self):
        return queries_for("Chinook Pass", [PoiCategory.WILD_CAMP])[0]

    def test_satisfies_the_protocol(self, source):
        assert isinstance(source, SearchSource)

    def test_it_declares_a_name(self, source):
        """Provenance: a candidate records which source claimed it."""
        assert source.name

    async def test_it_returns_candidates(self, source, query):
        found = await source.search(query, near=ANCHOR)
        assert all(candidate.name for candidate in found)

    async def test_every_candidate_is_tagged_with_the_querys_category(self, source, query):
        """Otherwise a fuel station ends up in the camping list."""
        found = await source.search(query, near=ANCHOR)
        assert all(candidate.category is query.category for candidate in found)

    async def test_every_candidate_records_the_anchor_it_was_found_near(self, source, query):
        """Our coordinate, not the source's. It is the only location worth trusting yet."""
        found = await source.search(query, near=ANCHOR)
        assert all(candidate.found_near == ANCHOR for candidate in found)

    async def test_every_candidate_records_this_source(self, source, query):
        found = await source.search(query, near=ANCHOR)
        assert all(candidate.source == source.name for candidate in found)

    async def test_no_candidate_arrives_pre_verified(self, source, query):
        """A source cannot verify anything. Only Places can, and it has not run yet."""
        found = await source.search(query, near=ANCHOR)
        assert all(not hasattr(candidate, "place_id") for candidate in found)

    async def test_an_empty_result_is_empty_not_an_error(self, source, query):
        """Most corridor-and-category pairs will genuinely find nothing."""
        assert isinstance(await source.search(query, near=ANCHOR), tuple)

    async def test_the_result_count_is_bounded(self, source, query):
        """Every candidate costs a Places lookup downstream, so a source cannot hand back
        a hundred of them and let the next stage pay for it."""
        found = await source.search(query, near=ANCHOR, limit=3)
        assert len(found) <= 3
