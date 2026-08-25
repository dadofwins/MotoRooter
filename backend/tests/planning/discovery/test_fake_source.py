"""The scripted search source, verified against the shared contract."""

import pytest

from motorooter.planning.discovery.errors import DiscoveryUnavailable
from motorooter.planning.discovery.protocol import SearchSource
from motorooter.planning.discovery.queries import queries_for
from motorooter.planning.discovery.sources.fake import FakeSearchSource
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory
from tests.planning.discovery.source_contract import ANCHOR, SearchSourceContract


class TestFakeSearchSource(SearchSourceContract):
    @pytest.fixture
    def source(self):
        return FakeSearchSource()


async def test_it_records_the_queries_it_was_given():
    """So a pipeline test can assert the fan-out rather than infer it from counts."""
    source = FakeSearchSource()
    for query in queries_for("Naches", [PoiCategory.FOOD, PoiCategory.FUEL]):
        await source.search(query, near=ANCHOR)
    assert [query.category for query in source.queries] == [
        PoiCategory.FOOD,
        PoiCategory.FUEL,
    ]


async def test_it_can_be_told_to_fail():
    """A real search engine will not 503 on request."""
    source = FakeSearchSource(error=DiscoveryUnavailable("upstream down"))
    with pytest.raises(DiscoveryUnavailable):
        await source.search(queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR)


async def test_it_satisfies_the_protocol():
    assert isinstance(FakeSearchSource(), SearchSource)


async def test_the_anchor_it_records_is_the_one_it_was_given():
    """Not one it invented, and not one from the query text."""
    elsewhere = Coordinate(lat=45.0, lon=-120.0)
    found = await FakeSearchSource().search(
        queries_for("Naches", [PoiCategory.FOOD])[0], near=elsewhere
    )
    assert all(candidate.found_near == elsewhere for candidate in found)
