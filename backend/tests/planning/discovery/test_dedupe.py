"""Not issuing the same search twice.

Two corridors have now shown it. Three anchors on Ellensburg-Cashmere all reverse-geocoded to
"Liberty"; two on Chinook Pass both to "Mather Memorial Parkway". Each duplicate is a metered
Brave request for an answer already in hand, and on a corridor that is one long road every
anchor gets the same name.

**This is deduplication, not caching, and the distinction is contractual rather than
pedantic.** Brave's terms permit "transient storage required for operation of Customer
Applications" and forbid everything beyond it, so results may not outlive the run that
fetched them. The deduplicator is therefore built per run and discarded with it: two anchors
that ask the same question during one replan share one answer, and the next replan asks
again.

That also means it cannot live in `build_discovery` beside the retry decorator, which is
constructed once and reused for the life of the process. A cache there would be exactly the
thing the terms prohibit.
"""

import asyncio

import pytest

from motorooter.planning.discovery.dedupe import DeduplicatingSearchSource
from motorooter.planning.discovery.errors import DiscoveryUnavailable
from motorooter.planning.discovery.models import Candidate
from motorooter.planning.discovery.queries import queries_for
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory

ANCHOR = Coordinate(lat=46.87, lon=-121.52)
NEARBY = Coordinate(lat=46.8701, lon=-121.5201)
ELSEWHERE = Coordinate(lat=47.50, lon=-120.46)


def query(place: str = "Liberty", category: PoiCategory = PoiCategory.WILD_CAMP):
    return queries_for(place, [category])[0]


class Counting:
    """Records every search it is asked to make."""

    name = "counting"

    def __init__(self, *, error: Exception | None = None, delay: float = 0.0):
        self.asked: list[tuple[str, str]] = []
        self.error = error
        self.delay = delay

    async def search(self, q, *, near, limit=5):
        self.asked.append((q.place, q.category.value))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return (
            Candidate(
                name=f"A place near {q.place}",
                category=q.category,
                found_near=near,
                source=self.name,
            ),
        )


class TestItAsksOnce:
    async def test_the_same_query_is_issued_once(self):
        source = Counting()
        deduped = DeduplicatingSearchSource(source)
        await deduped.search(query(), near=ANCHOR)
        await deduped.search(query(), near=ANCHOR)
        assert len(source.asked) == 1

    async def test_the_second_caller_still_gets_the_answer(self):
        source = Counting()
        deduped = DeduplicatingSearchSource(source)
        first = await deduped.search(query(), near=ANCHOR)
        second = await deduped.search(query(), near=ANCHOR)
        assert first == second
        assert second

    async def test_concurrent_callers_share_one_request(self):
        """The real case: anchors run in parallel, so the duplicates are simultaneous and a
        check-then-act would let both through."""
        source = Counting(delay=0.01)
        deduped = DeduplicatingSearchSource(source)
        await asyncio.gather(*(deduped.search(query(), near=ANCHOR) for _ in range(6)))
        assert len(source.asked) == 1

    async def test_a_different_category_is_a_different_query(self):
        source = Counting()
        deduped = DeduplicatingSearchSource(source)
        await deduped.search(query(category=PoiCategory.WILD_CAMP), near=ANCHOR)
        await deduped.search(query(category=PoiCategory.FOOD), near=ANCHOR)
        assert len(source.asked) == 2

    async def test_a_different_place_is_a_different_query(self):
        source = Counting()
        deduped = DeduplicatingSearchSource(source)
        await deduped.search(query("Liberty"), near=ANCHOR)
        await deduped.search(query("Cashmere"), near=ANCHOR)
        assert len(source.asked) == 2

    async def test_the_anchor_is_not_part_of_the_identity(self):
        """The duplicates this exists for are 25 km apart by construction — three anchors
        naming "Liberty" are spaced along a corridor. Keying on position would mean it never
        fired for the only case that motivated it."""
        source = Counting()
        deduped = DeduplicatingSearchSource(source)
        await deduped.search(query(), near=ANCHOR)
        await deduped.search(query(), near=ELSEWHERE)
        assert len(source.asked) == 1

    async def test_each_caller_gets_its_own_anchor_on_the_results(self):
        """`found_near` becomes the Places location bias and is the only trustworthy position
        a candidate has before resolution. Sharing the request must not share the provenance,
        or the second caller's lookup is biased 25 km away from where it asked."""
        deduped = DeduplicatingSearchSource(Counting())
        first = await deduped.search(query(), near=ANCHOR)
        second = await deduped.search(query(), near=ELSEWHERE)
        assert first[0].found_near == ANCHOR
        assert second[0].found_near == ELSEWHERE


class TestFailuresAreNotRemembered:
    async def test_a_failure_is_not_cached(self):
        """A retryable failure shared with every later caller would turn one bad moment into
        a corridor-wide outage — and the retry layer beneath would never get another go."""
        source = Counting(error=DiscoveryUnavailable("down"))
        deduped = DeduplicatingSearchSource(source)
        with pytest.raises(DiscoveryUnavailable):
            await deduped.search(query(), near=ANCHOR)
        with pytest.raises(DiscoveryUnavailable):
            await deduped.search(query(), near=ANCHOR)
        assert len(source.asked) == 2

    async def test_the_failure_reaches_every_concurrent_caller(self):
        source = Counting(error=DiscoveryUnavailable("down"), delay=0.01)
        deduped = DeduplicatingSearchSource(source)
        results = await asyncio.gather(
            *(deduped.search(query(), near=ANCHOR) for _ in range(3)),
            return_exceptions=True,
        )
        assert all(isinstance(item, DiscoveryUnavailable) for item in results)


class TestItIsScopedToOneRun:
    async def test_a_fresh_instance_asks_again(self):
        """Brave permits transient storage for the operation of the app and nothing more, so
        results must not outlive the run that fetched them."""
        source = Counting()
        await DeduplicatingSearchSource(source).search(query(), near=ANCHOR)
        await DeduplicatingSearchSource(source).search(query(), near=ANCHOR)
        assert len(source.asked) == 2

    async def test_the_name_passes_through(self):
        assert DeduplicatingSearchSource(Counting()).name == "counting"
