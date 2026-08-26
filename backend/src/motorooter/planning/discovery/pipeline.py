"""The discovery pipeline, end to end, as a stream of progress.

Five stages behind one call:

    name    anchor coordinate -> a searchable place name
    search  Brave -> pages about places
    extract LLM   -> the place names inside those pages
    resolve Places -> a real id, coordinate, category, and a distance filter
    judge   metrics + LLM -> a score and a reason

It yields as it goes rather than returning at the end, because discovery is slow and a
spinner is a worse answer than partial results — the map should fill in while the assistant
works. That is also why `ReplanEvent` carries POIs: each stage hands over what it has.

**Everything that can run at once does.** The work is almost entirely waiting on four APIs,
so doing it one request at a time spent minutes of wall clock with the CPU idle — a rider
pressed the button and watched a spinner for two minutes with an update every twenty-five
seconds, which reads as frozen rather than slow. Bounded, though: every provider here has a
per-minute ceiling, and exceeding one produces a wave of 429s indistinguishable from an
outage.

Progress is counted in completed units of work rather than in anchors, because with things
running in parallel "anchor 3 of 24" stops meaning anything.

**One implementation, two callers.** The REST endpoint and the assistant's tools both run
this. Item 5 of M1 — route through the found POIs — is reachable by button and by chat, and
two implementations of it would diverge silently, since both would produce something
plausible.

Nothing here raises for an ordinary failure. A corridor where one category finds nothing, or
one search times out, should cost those results rather than the run: the rest of it has
already been paid for in metered requests.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

from motorooter.llm.errors import LlmError
from motorooter.planning.discovery.category import CategoryClassifier
from motorooter.planning.discovery.concurrency import DEFAULT_CONCURRENCY
from motorooter.planning.discovery.corridor import (
    DEFAULT_MAX_ANCHORS,
    DISCOVERY_ANCHOR_SPACING_M,
    anchors,
)
from motorooter.planning.discovery.dedupe import DeduplicatingSearchSource
from motorooter.planning.discovery.errors import DiscoveryError
from motorooter.planning.discovery.expansion import (
    attribute,
    expansion_queries,
    roads_and_places,
)
from motorooter.planning.discovery.extract import PlaceExtractor
from motorooter.planning.discovery.judge import CandidateJudge
from motorooter.planning.discovery.models import (
    Candidate,
    ResolvedCandidate,
    ScoredCandidate,
)
from motorooter.planning.discovery.naming import PlaceNamer
from motorooter.planning.discovery.protocol import SearchSource
from motorooter.planning.discovery.queries import SearchQuery, queries_for
from motorooter.planning.discovery.resolve import PlacesResolver
from motorooter.routing.models import Coordinate, RouteLeg
from motorooter.trips.models import Poi, PoiCategory

logger = logging.getLogger(__name__)

SEARCH_STAGE = "discovery"
ENRICH_STAGE = "enrichment"
DONE_STAGE = "done"
"""Stage names from the frozen `ReplanEvent` vocabulary."""


@dataclass(frozen=True)
class DiscoveryProgress:
    """One step of a run, in the order it happened."""

    stage: str
    message: str
    progress: float | None = None
    pois: tuple[Poi, ...] = ()
    scored: tuple[ScoredCandidate, ...] = ()
    """The full judgement, for callers that want the evidence. The endpoint sends `pois`;
    the assistant's tools want the reasons too."""


NAMING_COST = 0.4
SEARCH_COST = 0.15
EXTRACT_COST = 1.4
RESOLVE_COST_PER_CANDIDATE = 0.07
JUDGE_BASE_COST = 6.0
JUDGE_COST_PER_CANDIDATE = 1.5
"""Roughly how many seconds each kind of work takes, from a live corridor.

Counting *steps* was the bug. A Brave search returning in 150 ms and a scoring call taking
fifteen seconds were worth one unit each, so the bar spent 91% of itself on the first third
of the wall clock and left the slowest stage two percentage points to move through:

    search + extract    9.4s    drove the bar 0 -> 91%
    resolve (37)        2.7s    91 -> 97%
    judge (6)          15.1s    97 -> 99%

These are estimates and they will drift — a faster model, a longer corridor, a different
provider all move them. That is tolerable in a way the old scheme was not: being wrong about
a stage's *share* skews the bar, while being wrong about its *existence* stops it dead. Any
figure within a factor of two of the truth beats counting steps.

Judging is deliberately the only one with a fixed cost as well as a per-candidate one: it is
a single call whose latency is mostly the model thinking, not the batch size.
"""

EXPECTED_NAMES_PER_SEARCH = 1.8
EXPECTED_RESOLVED_PER_SEARCH = 0.3
"""How much enrichment one search typically creates, so it can be costed before it exists.

From the same live corridor: twenty searches produced thirty-seven named candidates, of
which six survived resolution. Reserved up front rather than booked on discovery, because
booking it late is too late — by then the search phase has already consumed the whole bar,
and no amount of correcting the denominator afterwards can move a number that is already at
its ceiling.

The reserve is corrected against the real counts as soon as they are known, so a corridor
that yields nothing or twice the usual still ends up costed honestly. These figures only
decide how good the guess is before then.
"""

CEILING = 0.99
"""How close a non-terminal event may get to done. Only the final event says 1.0.

Raising this was suggested, on the grounds that the cap made every late event render the
same — and it was the wrong fix twice over. The tail was pinned because the *weights* were
wrong, not the ceiling; and a finer ceiling makes it worse, because 0.999 renders as "100%"
once a client rounds it, which claims completion while the run is still working. That is
exactly what the cap exists to prevent.

With the weights corrected, only the final pre-terminal event reaches the cap at all.
"""


@dataclass
class _Work:
    """Progress as completed cost against expected cost, both in rough seconds.

    Anchors made a poor denominator once the work ran in parallel — "anchor 3 of 24" stops
    describing anything when eight are in flight. Steps made a poor denominator too, for a
    subtler reason: a rider is waiting for *time*, and the steps here differ in cost by two
    orders of magnitude.
    """

    total: float
    done: float = 0.0

    def add(self, cost: float) -> None:
        """Book work that was not knowable up front, such as scoring N found candidates.

        Rescales `done` so the fraction is unchanged at the moment of the booking. Growing
        the denominator alone would send the bar *backwards* — 50% becoming 43% because the
        run discovered it had more to do — and a bar that retreats is worse than one that
        lies, because a rider cannot tell it from a restart.

        The cost of that choice is honest and small: the work already finished quietly
        becomes worth a smaller share, and everything remaining is redistributed across
        what is left. What is preserved is the property clients rely on, which is that this
        number only ever moves forwards.
        """
        if cost == 0:
            return
        fraction = self.done / self.total if self.total > 0 else 0.0
        self.total += cost
        self.done = fraction * self.total

    def step(self, stage: str, message: str, *, advance: float = 1.0) -> "DiscoveryProgress":
        self.done += advance
        fraction = min(self.done / self.total, CEILING) if self.total > 0 else CEILING
        return DiscoveryProgress(stage=stage, message=message, progress=fraction)


@dataclass
class _Counts:
    searched: int = 0
    named: int = 0
    resolved: int = 0
    uncategorised: int = 0
    """Resolved and scored, but with nothing Places would call a category.

    Road junctions and forest-road numbers, mostly — correctly unpinnable, and correctly
    dropped. Counted because they are dropped *after* a metered lookup and a scoring slot,
    so a run quietly paying for candidates it always meant to discard should say so.
    """

    expanded: int = 0
    """Results that came from following a road rather than from an anchor. The number that
    says whether expansion earned its extra searches."""

    failures: list[str] = field(default_factory=list)


class DiscoveryPipeline:
    """Runs discovery over a routed trip, yielding progress as it goes."""

    def __init__(
        self,
        *,
        namer: PlaceNamer,
        source: SearchSource,
        extractor: PlaceExtractor,
        resolver: PlacesResolver,
        classifier: CategoryClassifier,
        judge: CandidateJudge,
    ) -> None:
        self._namer = namer
        self._source = source
        self._extractor = extractor
        self._resolver = resolver
        self._classifier = classifier
        self._judge = judge

    async def run(
        self,
        leg: RouteLeg,
        categories: Sequence[PoiCategory],
        *,
        max_anchors: int = DEFAULT_MAX_ANCHORS,
        spacing_m: float = DISCOVERY_ANCHOR_SPACING_M,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> AsyncIterator[DiscoveryProgress]:
        """Discover POIs along a routed leg.

        Args:
            leg: the routed geometry to search along.
            categories: what to look for. Anchors times categories is the metered cost, and
                the caller decides — fuel every 25 km is noise, and the request already
                carries what the rider asked for.
            max_anchors: ceiling on search points.
            spacing_m: distance between them, coarser than the routing spacing on purpose.
            concurrency: requests in flight.

        Work runs in parallel and reports through a queue, so progress arrives as things
        finish rather than in the order they were started. A rider watching a spinner cannot
        tell slow from stuck, and the previous shape gave one update every twenty-five
        seconds.
        """
        placed = anchors(leg.geometry, spacing_m=spacing_m, max_anchors=max_anchors)
        wanted = list(dict.fromkeys(categories))
        if len(placed) < 2 or not wanted:
            yield DiscoveryProgress(stage=DONE_STAGE, message="nothing to search", progress=1.0)
            return

        counts = _Counts()
        # Per run, not per process. On a road-shaped corridor every anchor reverse-geocodes
        # to the same name, and each duplicate is a metered request for an answer already in
        # hand. Built here rather than in the factory because Brave permits only "transient
        # storage required for operation" — a deduplicator that outlived the run would be
        # the cache the terms forbid.
        source = DeduplicatingSearchSource(self._source)
        # Planned units: a lookup per anchor, a search per anchor and category, one
        # extraction per anchor, and three for enrichment — resolve, judge, and the tally.
        # Counting units rather than anchors is what keeps the percentage meaningful once
        # things overlap. Road-following books its own cost later, when it is known.
        searching = len(placed) * (NAMING_COST + EXTRACT_COST + len(wanted) * SEARCH_COST)
        searches = len(placed) * len(wanted)
        reserved_resolve = searches * EXPECTED_NAMES_PER_SEARCH * RESOLVE_COST_PER_CANDIDATE
        reserved_judge = (
            JUDGE_BASE_COST + searches * EXPECTED_RESOLVED_PER_SEARCH * JUDGE_COST_PER_CANDIDATE
        )
        work = _Work(total=searching + reserved_resolve + reserved_judge)
        updates: asyncio.Queue[DiscoveryProgress | None] = asyncio.Queue()
        found: list[Candidate] = []
        corridor_region: str | None = None
        """Whatever region an anchor resolved to, for disambiguating expansion searches.

        Any one will do: a corridor short enough to be one trip is not going to straddle two
        of them, and the alternative is threading a region through work that runs in parallel
        and finishes out of order.
        """

        async def handle(anchor: Coordinate) -> None:
            nonlocal corridor_region
            name, region = await self._describe(anchor, counts)
            if region is not None and corridor_region is None:
                corridor_region = region
            if name is None:
                # No name, no searches: hand back the budget so the bar does not stall.
                work.add(-(EXTRACT_COST + len(wanted) * SEARCH_COST))
                await updates.put(
                    work.step(SEARCH_STAGE, "nowhere named at one point", advance=NAMING_COST)
                )
                return
            await updates.put(
                work.step(SEARCH_STAGE, f"looking around {name}", advance=NAMING_COST)
            )

            results: list[Candidate] = []
            for query in queries_for(name, wanted):
                results.extend(await self._search(source, query, anchor, counts))
                await updates.put(
                    work.step(
                        SEARCH_STAGE,
                        f"searched {query.category.value} near {name}",
                        advance=SEARCH_COST,
                    )
                )

            extracted = await self._extract(results, region, name, counts)
            found.extend(extracted)
            await updates.put(
                work.step(
                    SEARCH_STAGE,
                    f"{len(extracted)} places named near {name}",
                    advance=EXTRACT_COST,
                )
            )

        async def produce() -> None:
            try:
                limit = asyncio.Semaphore(max(concurrency, 1))

                async def bounded(anchor: Coordinate) -> None:
                    async with limit:
                        await handle(anchor)

                await asyncio.gather(*(bounded(anchor) for anchor in placed))
            finally:
                await updates.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                update = await updates.get()
                if update is None:
                    break
                yield update
            await producer
        finally:
            # A client disconnecting mid-stream must not leave metered requests running for
            # a response nobody will read.
            producer.cancel()

        # A road is a lead, not a result: it becomes one more round of searches for the
        # places on it. Announced before it runs, because it is another slow stage and the
        # bar should say what it is waiting for.
        places, roads = roads_and_places(found)
        followed: tuple[Candidate, ...] = ()
        if roads:
            work.total += 1
            yield work.step(SEARCH_STAGE, f"{len(roads)} roads worth following: {_names(roads)}")
            followed = await self._follow_roads(roads, corridor_region, counts)

        # Three metered stages, three events. Behind one event this was fifteen seconds of
        # silence at 99% on a live corridor — the moment a rider is most likely to decide it
        # has hung. Their sizes are only knowable now, so this is where the rest of the bar
        # gets costed.
        leads = (*places, *followed)
        resolving = len(leads) * RESOLVE_COST_PER_CANDIDATE
        work.add(resolving - reserved_resolve)
        yield work.step(ENRICH_STAGE, f"checking {len(leads)} places are real", advance=0.0)
        resolved = await self._resolve(leads, leg, counts, concurrency)

        # Scoring is the slowest thing here by a wide margin — fifteen seconds against under
        # three for resolving four times as many — so it is named before it starts rather
        # than after it finishes. A bar that stops should say what it is waiting for.
        judging = JUDGE_BASE_COST + len(resolved) * JUDGE_COST_PER_CANDIDATE
        work.add(judging - reserved_judge)
        yield work.step(ENRICH_STAGE, f"scoring {len(resolved)} places", advance=resolving)

        # Scoring is one call and stays one call: the judge ranks candidates against each
        # other, so splitting the batch is what would cost the ranking. The increments come
        # from the reply streaming in, not from the work being divided — same call, same
        # ranking, and a number that moves instead of a flat half-minute at one position.
        scoring: asyncio.Queue[DiscoveryProgress | None] = asyncio.Queue()
        per_score = judging / max(len(resolved), 1)
        spent = 0.0

        def scored_one(done: int, total: int) -> None:
            nonlocal spent
            spent += per_score
            scoring.put_nowait(
                work.step(ENRICH_STAGE, f"scoring {done}/{total} places", advance=per_score)
            )

        async def score() -> tuple[ScoredCandidate, ...]:
            try:
                return await self._score(resolved, leg, counts, on_progress=scored_one)
            finally:
                scoring.put_nowait(None)

        scorer = asyncio.create_task(score())
        try:
            while True:
                update = await scoring.get()
                if update is None:
                    break
                yield update
            scored = await scorer
        finally:
            scorer.cancel()

        pois = tuple(_to_poi(item) for item in scored)
        # Whatever the per-score steps did not spend. Usually nothing is left; it is the
        # whole stage when the model could not be streamed, which is the case an older test
        # caught — without this the bar would sit still through scoring on any client
        # without a `stream`, which is exactly the complaint being fixed.
        yield work.step(ENRICH_STAGE, f"scored {len(scored)}", advance=max(judging - spent, 0.0))

        yield DiscoveryProgress(
            stage=DONE_STAGE,
            message=self._summary(counts, len(pois)),
            progress=1.0,
            pois=pois,
            scored=scored,
        )

    async def _describe(self, anchor: Coordinate, counts: _Counts) -> tuple[str | None, str | None]:
        try:
            return (
                await self._namer.name_for(anchor),
                await self._namer.region_for(anchor),
            )
        except DiscoveryError as exc:
            # One unnameable anchor is a gap in the corridor, not a failed run.
            counts.failures.append(f"naming: {exc}")
            return None, None

    async def _search(
        self,
        source: SearchSource,
        query: SearchQuery,
        anchor: Coordinate,
        counts: _Counts,
    ) -> list[Candidate]:
        try:
            results = await source.search(query, near=anchor)
        except DiscoveryError as exc:
            counts.failures.append(f"search {query.category.value}: {exc}")
            return []
        counts.searched += len(results)
        return list(results)

    async def _extract(
        self,
        results: Sequence[Candidate],
        region: str | None,
        name: str,
        counts: _Counts,
    ) -> tuple[Candidate, ...]:
        """One extraction per anchor, not per category.

        Every category around one place returns pages about the same neighbourhood, and a
        model reading them together is both cheaper and better placed to notice that three
        of them describe the same campsite. Nine calls became one.
        """
        if not results:
            return ()
        try:
            extracted = await self._extractor.extract(results, region=region, searched_for=name)
        except (DiscoveryError, LlmError) as exc:
            # "Fail fast" is only half the instruction. A timed-out extraction has to cost
            # this anchor's leads and nothing else — without this it aborted the corridor,
            # which the first live run of the parallel pipeline did immediately.
            counts.failures.append(f"extract near {name}: {exc}")
            return ()
        counts.named += len(extracted)
        return extracted

    async def _follow_roads(
        self,
        roads: Sequence[Candidate],
        region: str | None,
        counts: _Counts,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> tuple[Candidate, ...]:
        """One extra search round per road, for the places on it.

        The road's own evidence travels with whatever it surfaces, which is what makes this
        worth more than another search: a viewpoint on a road people ride for pleasure is
        worth more than the same viewpoint on a road nobody mentions.

        Concurrent and failure-tolerant for the same reasons every other stage here is. This
        is the last fan-out in the pipeline and the one most able to multiply — roads times
        queries — so running it serially would undo the speed work one stage from the end.
        """
        limit = asyncio.Semaphore(max(concurrency, 1))

        async def follow(road: Candidate, query: SearchQuery) -> tuple[Candidate, ...]:
            async with limit:
                try:
                    results = await self._source.search(query, near=road.found_near)
                except DiscoveryError as exc:
                    counts.failures.append(f"expansion {road.name}: {exc}")
                    return ()
                counts.searched += len(results)
                counts.expanded += len(results)
                try:
                    extracted = await self._extractor.extract(
                        results, region=region, searched_for=road.name
                    )
                except (DiscoveryError, LlmError) as exc:
                    counts.failures.append(f"expansion extract {road.name}: {exc}")
                    return ()
            return attribute(extracted, road)

        batches = await asyncio.gather(
            *(follow(road, query) for road in roads for query in expansion_queries(road))
        )
        surfaced = tuple(item for batch in batches for item in batch)
        counts.named += len(surfaced)
        return surfaced

    async def _resolve(
        self,
        candidates: Sequence[Candidate],
        leg: RouteLeg,
        counts: _Counts,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> tuple[ResolvedCandidate, ...]:
        """Claims become facts, or they are dropped."""
        if not candidates:
            return ()
        try:
            resolved = await self._resolver.resolve(
                candidates, route=leg.geometry, concurrency=concurrency
            )
            resolved = await self._classifier.classify(resolved)
        except (DiscoveryError, LlmError) as exc:
            counts.failures.append(f"resolve: {exc}")
            return ()

        resolved = _unique(resolved)
        counts.resolved = len(resolved)

        # Anything Places will not categorise can never be pinned, so scoring it buys
        # nothing. Dropped here rather than after judging: a road junction was costing a
        # slot in every batch, and the judge is measurably less reliable on larger ones.
        pinnable = tuple(item for item in resolved if item.category is not None)
        counts.uncategorised = len(resolved) - len(pinnable)
        return pinnable

    async def _score(
        self,
        resolved: Sequence[ResolvedCandidate],
        leg: RouteLeg,
        counts: _Counts,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[ScoredCandidate, ...]:
        if not resolved:
            return ()
        try:
            scored = await self._judge.judge(resolved, leg, on_progress=on_progress)
        except (DiscoveryError, LlmError) as exc:
            # Everything up to here is already paid for. Losing the scores is bad; losing
            # the run because scoring timed out is worse.
            counts.failures.append(f"judge: {exc}")
            return ()

        # Scoring none of them is a malfunction, not an opinion. `judge` returns empty rather
        # than raising on an unusable reply, which is right — but silently, and one live run
        # in four came back this way: eight candidates on the route, nothing scored, and a
        # summary reading "0 worth showing" that is indistinguishable from an empty corridor.
        # Scoring *some* of them is a judgement and stays quiet.
        if not scored:
            counts.failures.append(f"judge: scored none of {len(resolved)} places")
        return scored

    @staticmethod
    def _summary(counts: _Counts, pinned: int) -> str:
        parts = [
            f"{counts.searched} results",
            f"{counts.named} named",
            f"{counts.resolved} on the route",
            f"{pinned} worth showing",
        ]
        if counts.uncategorised:
            parts.append(f"{counts.uncategorised} with no category")
        if counts.expanded:
            parts.append(f"{counts.expanded} of the results came from following roads")
        if counts.failures:
            parts.append(f"{len(counts.failures)} stage failures")
        return ", ".join(parts)


def _unique(resolved: Sequence[ResolvedCandidate]) -> tuple[ResolvedCandidate, ...]:
    """One entry per real place.

    Adjacent anchors search overlapping ground, so the same campsite is found two or three
    times under slightly different names — "Shriner Peak" and "Shriner Peak, Washington"
    came back as separate pins on a live run. Extraction deduplicates within a batch and
    cannot see across them; `place_id` is the identity that settles it, and it is exactly
    what resolution exists to produce.

    Deduplicating here rather than after judging also saves scoring the same place twice.
    """
    seen: set[str] = set()
    unique: list[ResolvedCandidate] = []
    for candidate in resolved:
        if candidate.place_id in seen:
            continue
        seen.add(candidate.place_id)
        unique.append(candidate)
    return tuple(unique)


def _to_poi(scored: ScoredCandidate) -> Poi:
    """A pinnable POI, carrying the judge's reason as its note.

    Not pinned to the route: discovery proposes, and putting something *on* the route is a
    separate decision the rider makes.
    """
    return scored.resolved.to_poi(poi_id=str(uuid.uuid4()), note=scored.reason)


def _names(candidates: Sequence[Candidate], limit: int = 3) -> str:
    """A short, readable list for a progress message."""
    shown = [candidate.name for candidate in candidates[:limit]]
    if len(candidates) > limit:
        shown.append(f"and {len(candidates) - limit} more")
    return ", ".join(shown)
