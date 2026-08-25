"""The whole pipeline, driven by fakes.

What matters here is not that each stage works — each has its own tests — but that a run
survives its parts failing. Discovery makes dozens of metered requests across five stages
and four vendors; a corridor where one search times out or one anchor cannot be named should
cost those results, not the run that has already paid for the rest.
"""

from math import pi

from motorooter.llm.messages import AssistantMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.planning.discovery.category import CategoryClassifier
from motorooter.planning.discovery.errors import DiscoveryUnavailable
from motorooter.planning.discovery.extract import PlaceExtractor
from motorooter.planning.discovery.judge import CandidateJudge
from motorooter.planning.discovery.naming import PlaceNamer
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.planning.discovery.resolve import PlacesResolver
from motorooter.planning.discovery.sources.fake import FakeSearchSource
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import PoiCategory

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180

LEG = RouteLeg(
    geometry=tuple(
        Coordinate(lat=index * 500.0 / M_PER_DEGREE_LAT, lon=-121.0) for index in range(60)
    ),
    distance_m=30_000.0,
    duration_s=1800.0,
    provider="fake",
    intent=LegIntent.UNPAVED,
)
CATEGORIES = [PoiCategory.WILD_CAMP]


class StubNamer(PlaceNamer):
    def __init__(self, *, name: str | None = "Chinook Pass", error: Exception | None = None):
        super().__init__(api_key="unused")
        self._name = name
        self._error = error

    async def name_for(self, anchor):
        if self._error:
            raise self._error
        return self._name

    async def region_for(self, anchor):
        return "Washington"


class StubResolver(PlacesResolver):
    def __init__(self, *, resolved=(), error=None):
        super().__init__(api_key="unused")
        self._resolved = resolved
        self._error = error

    async def resolve(self, candidates, *, route=(), corridor_m=15_000.0):
        if self._error:
            raise self._error
        return self._resolved


def llm(*replies: str) -> FakeLlmClient:
    return FakeLlmClient(
        replies=tuple(AssistantMessage(content=reply) for reply in replies), repeat_last=True
    )


def pipeline(*, namer=None, source=None, resolver=None, client=None) -> DiscoveryPipeline:
    model = client or llm('{"places": []}')
    return DiscoveryPipeline(
        namer=namer or StubNamer(),
        source=source or FakeSearchSource(),
        extractor=PlaceExtractor(model),
        resolver=resolver or StubResolver(),
        classifier=CategoryClassifier(model),
        judge=CandidateJudge(model),
    )


async def collect(runner: DiscoveryPipeline, **kwargs):
    return [event async for event in runner.run(LEG, CATEGORIES, **kwargs)]


class TestAWholeRun:
    async def test_it_ends_with_a_done_event(self):
        events = await collect(pipeline())
        assert events[-1].stage == "done"

    async def test_it_reports_progress_as_it_goes(self):
        """A spinner is a worse answer than partial results."""
        events = await collect(pipeline())
        assert any(event.stage == "discovery" for event in events)

    async def test_progress_increases_towards_one(self):
        events = await collect(pipeline())
        values = [event.progress for event in events if event.progress is not None]
        assert values == sorted(values)
        assert values[-1] == 1.0

    async def test_it_searches_once_per_anchor_per_category(self):
        source = FakeSearchSource()
        await collect(pipeline(source=source), max_anchors=4)
        assert len(source.queries) == 4 * len(CATEGORIES)

    async def test_the_anchor_budget_is_respected(self):
        """Anchors times categories is the metered cost of a run."""
        source = FakeSearchSource()
        await collect(pipeline(source=source), max_anchors=2)
        assert len(source.queries) == 2

    async def test_an_unrouted_leg_ends_immediately(self):
        empty = DiscoveryPipeline(
            namer=StubNamer(),
            source=FakeSearchSource(),
            extractor=PlaceExtractor(llm('{"places": []}')),
            resolver=StubResolver(),
            classifier=CategoryClassifier(llm('{"categories": []}')),
            judge=CandidateJudge(llm('{"scores": []}')),
        )
        events = [
            event
            async for event in empty.run(
                RouteLeg(
                    geometry=(Coordinate(lat=0, lon=0), Coordinate(lat=0, lon=0)),
                    distance_m=0.0,
                    duration_s=0.0,
                    provider="fake",
                    intent=LegIntent.UNPAVED,
                ),
                CATEGORIES,
            )
        ]
        assert events[-1].stage == "done"


class TestItSurvivesItsPartsFailing:
    async def test_an_unnameable_anchor_does_not_end_the_run(self):
        events = await collect(
            pipeline(namer=StubNamer(error=DiscoveryUnavailable("geocoder down")))
        )
        assert events[-1].stage == "done"

    async def test_a_failing_search_does_not_end_the_run(self):
        events = await collect(
            pipeline(source=FakeSearchSource(error=DiscoveryUnavailable("brave down")))
        )
        assert events[-1].stage == "done"

    async def test_a_failing_resolve_does_not_end_the_run(self):
        events = await collect(
            pipeline(resolver=StubResolver(error=DiscoveryUnavailable("places down")))
        )
        assert events[-1].stage == "done"

    async def test_failures_are_counted_in_the_summary(self):
        """Silent partial results are the worst outcome: it looks like an empty corridor."""
        events = await collect(
            pipeline(source=FakeSearchSource(error=DiscoveryUnavailable("brave down")))
        )
        assert "failure" in events[-1].message

    async def test_a_clean_run_does_not_mention_failures(self):
        events = await collect(pipeline())
        assert "failure" not in events[-1].message


class TestDuplicatesAcrossAnchors:
    """Adjacent anchors search overlapping ground; extraction cannot see across batches."""

    async def test_the_same_place_is_pinned_once(self):
        from motorooter.planning.discovery.models import Candidate, ResolvedCandidate

        def twice(name: str) -> ResolvedCandidate:
            return ResolvedCandidate(
                candidate=Candidate(
                    name=name,
                    category=PoiCategory.WILD_CAMP,
                    found_near=Coordinate(lat=0.01, lon=-121.0),
                    source="brave",
                ),
                place_id="ChIJ_same",
                coordinate=Coordinate(lat=0.01, lon=-121.0),
                category=PoiCategory.WILD_CAMP,
            )

        # "First" appears in the fake source's titles, so extraction is grounded and the
        # pipeline reaches resolve at all.
        extracting = llm('{"places": [{"result_index": 0, "place_name": "First"}]}')
        scoring = llm('{"scores": [{"index": 0, "score": 0.8, "reason": "good"}]}')
        runner = DiscoveryPipeline(
            namer=StubNamer(),
            source=FakeSearchSource(),
            extractor=PlaceExtractor(extracting),
            resolver=StubResolver(
                resolved=(twice("Shriner Peak"), twice("Shriner Peak, Washington"))
            ),
            classifier=CategoryClassifier(llm('{"categories": []}')),
            judge=CandidateJudge(scoring),
        )
        events = [event async for event in runner.run(LEG, CATEGORIES, max_anchors=2)]
        assert len(events[-1].pois) == 1


class TestWhatItHandsBack:
    async def test_an_uncategorised_place_is_not_pinned(self):
        """`to_poi` refuses it, and the pipeline must not ask."""
        events = await collect(pipeline())
        assert all(poi.category is not None for event in events for poi in event.pois)

    async def test_pois_are_not_pinned_to_the_route(self):
        """Discovery proposes. Putting something on the route is the rider's decision."""
        events = await collect(pipeline())
        assert all(not poi.on_route for event in events for poi in event.pois)

    async def test_the_summary_reports_the_funnel(self):
        """The drop rate is the signal that says whether the queries are working."""
        events = await collect(pipeline())
        assert "named" in events[-1].message
