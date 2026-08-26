"""The whole pipeline, driven by fakes.

What matters here is not that each stage works — each has its own tests — but that a run
survives its parts failing. Discovery makes dozens of metered requests across five stages
and four vendors; a corridor where one search times out or one anchor cannot be named should
cost those results, not the run that has already paid for the rest.
"""

import json
from math import pi

from motorooter.llm.errors import LlmUnavailable
from motorooter.llm.messages import AssistantMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.planning.discovery.category import CategoryClassifier
from motorooter.planning.discovery.errors import DiscoveryUnavailable
from motorooter.planning.discovery.extract import PlaceExtractor
from motorooter.planning.discovery.judge import CandidateJudge
from motorooter.planning.discovery.models import Candidate, ResolvedCandidate
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
    """Names each anchor distinctly by default.

    A namer that returned one name for every anchor was accidentally degenerate once
    duplicate queries stopped being issued twice: tests meaning "one search per anchor" were
    really measuring "one search per distinct place name", and passed only because nothing
    collapsed them. Pass an explicit `name` to get the repeated case on purpose.
    """

    def __init__(self, *, name: str | None = None, error: Exception | None = None):
        super().__init__(api_key="unused")
        self._name = name
        self._error = error

    async def name_for(self, anchor):
        if self._error:
            raise self._error
        if self._name is not None:
            return self._name
        return f"Place at {anchor.lat:.4f}"

    async def region_for(self, anchor):
        return "Washington"


class StubResolver(PlacesResolver):
    def __init__(self, *, resolved=(), error=None):
        super().__init__(api_key="unused")
        self._resolved = resolved
        self._error = error

    async def resolve(self, candidates, *, route=(), corridor_m=15_000.0, concurrency=6):
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
        await collect(pipeline(source=source), max_anchors=4, spacing_m=1000)
        assert len(source.queries) == 4 * len(CATEGORIES)

    async def test_the_anchor_budget_is_respected(self):
        """Anchors times categories is the metered cost of a run."""
        source = FakeSearchSource()
        await collect(pipeline(source=source), max_anchors=2, spacing_m=1000)
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
        events = [
            event async for event in runner.run(LEG, CATEGORIES, max_anchors=2, spacing_m=1000)
        ]
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


class TestItDoesNotMakeARiderWait:
    """The shape Tim reported: a spinner with an update every twenty-five seconds.

    Two causes, both addressed here. The work ran one request at a time while waiting on
    four APIs, and extraction fired once per category when every category around one place
    returns pages about the same neighbourhood.
    """

    async def test_anchors_are_searched_concurrently(self):
        import asyncio

        live = 0
        peak = 0

        class Counting(FakeSearchSource):
            async def search(self, query, *, near, limit=5):
                nonlocal live, peak
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.01)
                live -= 1
                return await super().search(query, near=near, limit=limit)

        await collect(pipeline(source=Counting()), max_anchors=6, spacing_m=1000)
        assert peak > 1

    async def test_extraction_happens_once_per_anchor_not_per_category(self):
        """Nine calls for one neighbourhood wanted to be one, and it is close to a 9x cut."""
        client = llm('{"places": []}')
        runner = DiscoveryPipeline(
            namer=StubNamer(),
            source=FakeSearchSource(),
            extractor=PlaceExtractor(client),
            resolver=StubResolver(),
            classifier=CategoryClassifier(llm('{"categories": []}')),
            judge=CandidateJudge(llm('{"scores": []}')),
        )
        three = [PoiCategory.WILD_CAMP, PoiCategory.FOOD, PoiCategory.FUEL]
        events = [event async for event in runner.run(LEG, three, max_anchors=2, spacing_m=1000)]
        assert events
        assert client.call_count == 2

    async def test_progress_arrives_per_category_not_per_anchor(self):
        """Tim's words: "searching the web..., found 4 web sites, judging 1 of 7"."""
        events = await collect(pipeline(), max_anchors=2, spacing_m=1000)
        searching = [event for event in events if event.stage == "discovery"]
        assert len(searching) > 2 * len(CATEGORIES)

    async def test_progress_never_goes_backwards(self):
        """Out-of-order completion must not make the bar jump about."""
        events = await collect(pipeline(), max_anchors=4, spacing_m=1000)
        values = [event.progress for event in events if event.progress is not None]
        assert values == sorted(values)

    async def test_progress_reaches_one_only_at_the_end(self):
        events = await collect(pipeline(), max_anchors=3, spacing_m=1000)
        assert events[-1].progress == 1.0
        assert all((event.progress or 0) < 1.0 for event in events[:-1])

    async def test_an_unnameable_anchor_returns_its_share_of_the_budget(self):
        """Otherwise the bar stalls short of the end for a corridor that finished."""
        events = await collect(
            pipeline(namer=StubNamer(error=DiscoveryUnavailable("geocoder down"))),
            max_anchors=3,
            spacing_m=1000,
        )
        assert events[-1].progress == 1.0

    async def test_discovery_spacing_is_coarser_than_routing_spacing(self):
        """A rider does not need a fresh search every 12.5 km; that is 216 for a 300 km trip."""
        from motorooter.planning.discovery.corridor import (
            DEFAULT_ANCHOR_SPACING_M,
            DISCOVERY_ANCHOR_SPACING_M,
        )

        assert DISCOVERY_ANCHOR_SPACING_M > DEFAULT_ANCHOR_SPACING_M


class TestAStageFailureCostsTheStageNotTheRun:
    """Found by running it: a timed-out extraction aborted the whole corridor.

    "Fail fast" is only half the instruction. The other half is moving on, and an `LlmError`
    is a different hierarchy from `DiscoveryError` — so the handler that caught search
    failures let extraction failures straight through.
    """

    @staticmethod
    def _failing() -> FakeLlmClient:
        """A model that times out, which is how the live run failed."""
        return FakeLlmClient(error=LlmUnavailable("request to OpenAI failed"))

    async def test_a_failed_extraction_does_not_end_the_run(self):
        runner = DiscoveryPipeline(
            namer=StubNamer(),
            source=FakeSearchSource(),
            extractor=PlaceExtractor(self._failing()),
            resolver=StubResolver(),
            classifier=CategoryClassifier(llm('{"categories": []}')),
            judge=CandidateJudge(llm('{"scores": []}')),
        )
        events = [
            event async for event in runner.run(LEG, CATEGORIES, max_anchors=2, spacing_m=1000)
        ]
        assert events[-1].stage == "done"
        assert "failure" in events[-1].message

    async def test_a_failed_judgement_does_not_end_the_run(self):
        """Everything before it is already paid for in metered requests.

        The resolver has to return something for this to test anything: an empty candidate
        list short-circuits `_enrich` before the judge is reached, and the first version of
        this test passed against no judge error handling at all for exactly that reason.
        """
        resolved = ResolvedCandidate(
            candidate=Candidate(
                name="First Camp",
                category=PoiCategory.WILD_CAMP,
                found_near=Coordinate(lat=0.01, lon=-121.0),
                source="brave",
            ),
            place_id="ChIJ_judged",
            coordinate=Coordinate(lat=0.01, lon=-121.0),
            category=PoiCategory.WILD_CAMP,
        )
        runner = DiscoveryPipeline(
            namer=StubNamer(),
            source=FakeSearchSource(),
            extractor=PlaceExtractor(
                llm('{"places": [{"result_index": 0, "place_name": "First", "relevant": true}]}')
            ),
            resolver=StubResolver(resolved=(resolved,)),
            classifier=CategoryClassifier(llm('{"categories": []}')),
            judge=CandidateJudge(self._failing()),
        )
        events = [
            event async for event in runner.run(LEG, CATEGORIES, max_anchors=2, spacing_m=1000)
        ]
        assert events[-1].stage == "done"
        assert "failure" in events[-1].message

    async def test_the_timeout_leaves_room_above_measured_latency(self):
        """A limit below normal latency does not fail fast, it fails always.

        Two numbers were set before either was measured, and both timed out on ordinary
        calls. Live, a batch of fifteen snippets at `EXTRACT_EFFORT` takes 2.9-3.4s, so the
        budget wants to be several times that rather than a round number near it.
        """
        from motorooter.planning.discovery.factory import EXTRACT_TIMEOUT_S

        measured_worst_s = 3.4
        assert 3 * measured_worst_s <= EXTRACT_TIMEOUT_S


class TestEnrichmentReportsProgressToo:
    """The silence that was left after the searches were made fast.

    A live corridor: searching and naming finished at 9.6s with steady updates, then the bar
    sat at 99% for fifteen seconds saying "checking 15 places are real" while resolve,
    classification and judging ran. Three metered stages behind one event, at the exact
    moment a rider is most likely to conclude it has hung.
    """

    @staticmethod
    def _one_real_place():
        return ResolvedCandidate(
            candidate=Candidate(
                name="First",
                category=PoiCategory.WILD_CAMP,
                found_near=Coordinate(lat=0.01, lon=-121.0),
                source="brave",
            ),
            place_id="ChIJ_first",
            coordinate=Coordinate(lat=0.01, lon=-121.0),
            category=PoiCategory.WILD_CAMP,
        )

    async def _events(self):
        runner = DiscoveryPipeline(
            namer=StubNamer(),
            source=FakeSearchSource(),
            extractor=PlaceExtractor(
                llm('{"places": [{"result_index": 0, "place_name": "First"}]}')
            ),
            resolver=StubResolver(resolved=(self._one_real_place(),)),
            classifier=CategoryClassifier(llm('{"categories": []}')),
            judge=CandidateJudge(llm('{"scores": [{"index": 0, "score": 0.8, "reason": "ok"}]}')),
        )
        return [e async for e in runner.run(LEG, CATEGORIES, max_anchors=2, spacing_m=1000)]

    async def test_enrichment_is_more_than_one_event(self):
        events = await self._events()
        enriching = [e for e in events if e.stage == "enrichment"]
        assert len(enriching) > 1, "the slowest stretch of the run reports once"

    async def test_it_says_what_it_is_doing_in_each_step(self):
        """ "Checking 15 places are real" covers three different metered stages."""
        events = await self._events()
        said = " ".join(e.message for e in events if e.stage == "enrichment").lower()
        assert "scor" in said or "judg" in said

    async def test_progress_still_only_reaches_one_at_the_end(self):
        events = await self._events()
        assert [e.progress for e in events].count(1.0) == 1
        assert events[-1].progress == 1.0


class TestNothingIsLostWithoutSaying:
    """Two silent losses, both found by running the pipeline rather than reading it.

    On four live runs of one corridor, one produced zero POIs from eight resolved,
    on-route candidates and reported no failure at all — the summary said "0 worth showing",
    which a rider reads as "nothing here" rather than "the model returned nothing". The other
    three quietly discarded one or two candidates apiece for having no category, after paying
    Places to resolve them and the judge to score them.

    Neither is a crash and neither should be. Both have to be *counted*, because a stage that
    loses everything and a corridor that contains nothing look identical from the outside,
    and only one of them is worth a rider's attention.
    """

    @staticmethod
    def _resolved(name: str, place_id: str, category):
        return ResolvedCandidate(
            candidate=Candidate(
                name=name,
                category=PoiCategory.WILD_CAMP,
                found_near=Coordinate(lat=0.01, lon=-121.0),
                source="brave",
            ),
            place_id=place_id,
            coordinate=Coordinate(lat=0.01, lon=-121.0),
            category=category,
        )

    async def _run(self, *, resolved, scores: str):
        runner = DiscoveryPipeline(
            namer=StubNamer(),
            source=FakeSearchSource(),
            extractor=PlaceExtractor(
                llm('{"places": [{"result_index": 0, "place_name": "First"}]}')
            ),
            resolver=StubResolver(resolved=resolved),
            classifier=CategoryClassifier(llm('{"categories": []}')),
            judge=CandidateJudge(llm(scores)),
        )
        return [e async for e in runner.run(LEG, CATEGORIES, max_anchors=2, spacing_m=1000)]

    async def test_scoring_nothing_at_all_is_reported_as_a_failure(self):
        """The one-in-four case. A model that answers with prose costs the batch its scores,
        which is by design — saying nothing about it is not."""
        events = await self._run(
            resolved=(self._resolved("First", "ChIJ_a", PoiCategory.WILD_CAMP),),
            scores='{"scores": []}',
        )
        assert "failure" in events[-1].message

    async def test_scoring_some_of_them_is_not_a_failure(self):
        """A model declining to score one place is an opinion, not a malfunction."""
        events = await self._run(
            resolved=(
                self._resolved("First", "ChIJ_a", PoiCategory.WILD_CAMP),
                self._resolved("Second", "ChIJ_b", PoiCategory.WILD_CAMP),
            ),
            scores='{"scores": [{"index": 0, "score": 0.8, "reason": "good"}]}',
        )
        assert "failure" not in events[-1].message

    async def test_uncategorisable_places_are_counted_not_just_dropped(self):
        """A road junction is correctly unpinnable, and was costing a Places lookup and a
        scoring slot before vanishing without trace."""
        events = await self._run(
            resolved=(self._resolved("Sunset Way & 6th Ave NE", "ChIJ_x", None),),
            scores='{"scores": [{"index": 0, "score": 0.8, "reason": "good"}]}',
        )
        assert "categor" in events[-1].message
        # And not as a judge failure: the judge was never asked, because a candidate that
        # cannot be pinned is dropped before scoring rather than after.
        assert "failure" not in events[-1].message

    async def test_a_clean_run_mentions_neither(self):
        events = await self._run(
            resolved=(self._resolved("First", "ChIJ_a", PoiCategory.WILD_CAMP),),
            scores='{"scores": [{"index": 0, "score": 0.8, "reason": "good"}]}',
        )
        assert "failure" not in events[-1].message
        assert "categor" not in events[-1].message


class TestTheBarTracksTimeNotSteps:
    """Tim, twice now: "it got to 99% and then took like an entire minute".

    Measured on a live corridor, one event at a time:

        search + extract    9.4s   drove the bar 0 -> 91%
        resolve (37)        2.7s   91 -> 97%
        judge (6)          15.1s   97 -> 99%

    So the slowest stage owned two percentage points and the fastest owned ninety-one. The
    denominator counted *steps*, and steps are not what a rider is waiting for — a search
    that returns in 150 ms and a scoring call that takes fifteen seconds were worth one unit
    each.

    Weighting by candidate count, which is the obvious fix, would have missed it: resolution
    scales with candidates and was never the problem.
    """

    @staticmethod
    def _resolved(index: int):
        return ResolvedCandidate(
            candidate=Candidate(
                name=f"Place {index}",
                category=PoiCategory.WILD_CAMP,
                found_near=Coordinate(lat=0.01, lon=-121.0),
                source="brave",
            ),
            place_id=f"ChIJ_{index}",
            coordinate=Coordinate(lat=0.01, lon=-121.0),
            category=PoiCategory.WILD_CAMP,
        )

    async def _events(self, *, resolved_count: int = 3, anchors: int = 4):
        scores = json.dumps(
            {
                "scores": [
                    {"index": i, "score": 0.8, "reason": "good"} for i in range(resolved_count)
                ]
            }
        )
        runner = DiscoveryPipeline(
            namer=StubNamer(),
            source=FakeSearchSource(),
            extractor=PlaceExtractor(
                llm('{"places": [{"result_index": 0, "place_name": "First"}]}')
            ),
            resolver=StubResolver(resolved=tuple(self._resolved(i) for i in range(resolved_count))),
            classifier=CategoryClassifier(llm('{"categories": []}')),
            judge=CandidateJudge(llm(scores)),
        )
        return [
            event
            async for event in runner.run(LEG, CATEGORIES, max_anchors=anchors, spacing_m=1000)
        ]

    async def test_searching_does_not_consume_almost_the_whole_bar(self):
        """91% for a third of the wall clock is what made the tail look frozen."""
        events = await self._events()
        last_search = max(
            event.progress for event in events if event.stage == "discovery" and event.progress
        )
        assert last_search < 0.8

    async def test_scoring_owns_a_share_worth_watching(self):
        """The stage that takes the longest should move the bar the furthest."""
        events = await self._events()
        enrichment = [e.progress for e in events if e.stage == "enrichment" and e.progress]
        before = max(e.progress for e in events if e.stage == "discovery" and e.progress)
        assert enrichment[-1] - before > 0.15

    async def test_progress_never_reaches_one_before_the_end(self):
        events = await self._events()
        assert all(event.progress < 1.0 for event in events[:-1] if event.progress is not None)
        assert events[-1].progress == 1.0

    async def test_no_early_event_rounds_up_to_a_hundred_percent(self):
        """A client renders this as a percentage. 0.999 is below 1.0 and still displays as
        "100%", which claims completion while the run is still scoring — so the ceiling has
        to leave room for the rounding, not just for the comparison."""
        events = await self._events()
        assert all(
            round(event.progress * 100) < 100 for event in events[:-1] if event.progress is not None
        )

    async def test_it_still_only_moves_forwards(self):
        events = await self._events()
        values = [event.progress for event in events if event.progress is not None]
        assert values == sorted(values)

    async def test_the_tail_is_not_pinned_to_a_single_value(self):
        """The 0.99 cap made every late event render identically, which is precisely where
        the waiting now happens."""
        events = await self._events()
        late = [
            event.progress
            for event in events
            if event.progress is not None and event.progress > 0.9
        ]
        assert len(set(late)) > 1

    async def test_the_rider_is_told_scoring_is_next(self):
        """A bar that stops for fifteen seconds should say what it is waiting for."""
        events = await self._events()
        messages = " ".join(e.message for e in events).lower()
        assert "scoring" in messages


class TestDuplicateQueriesAreNotPaidForTwice:
    """Anchors that name the same place should search it once.

    Measured on two corridors: three anchors on Ellensburg-Cashmere all reverse-geocoded to
    "Liberty", two on Chinook Pass to "Mather Memorial Parkway". On a road-shaped corridor
    every anchor gets the same name, and each duplicate is a metered request for an answer
    already in hand.

    Deduplicated per run rather than cached, because Brave's terms permit only "transient
    storage required for operation" — so the next replan asks again.
    """

    async def test_anchors_with_the_same_name_search_once(self):
        source = FakeSearchSource()
        await collect(
            pipeline(namer=StubNamer(name="Liberty"), source=source),
            max_anchors=4,
            spacing_m=1000,
        )
        # Four anchors, all named "Liberty", one category: one search rather than four.
        assert len(source.queries) == len(CATEGORIES)

    async def test_distinct_names_still_search_separately(self):
        """The saving must not come from asking fewer real questions."""
        names = iter(["Liberty", "Cashmere", "Cle Elum", "Blewett Pass"])

        class Varying(StubNamer):
            async def name_for(self, anchor):
                return next(names, "Elsewhere")

        source = FakeSearchSource()
        await collect(pipeline(namer=Varying(), source=source), max_anchors=4, spacing_m=1000)
        assert len(source.queries) == 4 * len(CATEGORIES)

    async def test_a_later_run_asks_again(self):
        """Per run, not per process. Results may not outlive the run that fetched them."""
        source = FakeSearchSource()
        for _ in range(2):
            await collect(
                pipeline(namer=StubNamer(name="Liberty"), source=source),
                max_anchors=2,
                spacing_m=1000,
            )
        assert len(source.queries) == 2 * len(CATEGORIES)
