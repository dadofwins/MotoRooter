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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from motorooter.llm.errors import LlmError
from motorooter.planning.discovery.category import CategoryClassifier
from motorooter.planning.discovery.concurrency import DEFAULT_CONCURRENCY
from motorooter.planning.discovery.corridor import (
    DEFAULT_MAX_ANCHORS,
    DISCOVERY_ANCHOR_SPACING_M,
    anchors,
)
from motorooter.planning.discovery.errors import DiscoveryError
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


@dataclass
class _Work:
    """Progress as completed units against planned units.

    Anchors made a poor denominator once the work ran in parallel — "anchor 3 of 24" stops
    describing anything when eight are in flight. Units are what a rider is waiting for:
    every lookup, every search, and the enrichment at the end.
    """

    total: int
    done: int = 0

    def step(self, stage: str, message: str, *, advance: int = 1) -> "DiscoveryProgress":
        self.done += advance
        fraction = min(self.done / self.total, 0.99) if self.total > 0 else 0.99
        return DiscoveryProgress(stage=stage, message=message, progress=fraction)


@dataclass
class _Counts:
    searched: int = 0
    named: int = 0
    resolved: int = 0
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
        # Planned units: a lookup per anchor, a search per anchor and category, one
        # extraction per anchor, and three for enrichment — resolve, judge, and the tally.
        # Counting units rather than anchors is what keeps the percentage meaningful once
        # things overlap.
        work = _Work(total=len(placed) * (2 + len(wanted)) + 3)
        updates: asyncio.Queue[DiscoveryProgress | None] = asyncio.Queue()
        found: list[Candidate] = []

        async def handle(anchor: Coordinate) -> None:
            name, region = await self._describe(anchor, counts)
            if name is None:
                # No name, no searches: hand back the budget so the bar does not stall.
                work.total -= 1 + len(wanted)
                await updates.put(work.step(SEARCH_STAGE, "nowhere named at one point"))
                return
            await updates.put(work.step(SEARCH_STAGE, f"looking around {name}"))

            results: list[Candidate] = []
            for query in queries_for(name, wanted):
                results.extend(await self._search(query, anchor, counts))
                await updates.put(
                    work.step(SEARCH_STAGE, f"searched {query.category.value} near {name}")
                )

            extracted = await self._extract(results, region, name, counts)
            found.extend(extracted)
            await updates.put(work.step(SEARCH_STAGE, f"{len(extracted)} places named near {name}"))

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

        # Three metered stages, three events. Behind one event this was fifteen seconds of
        # silence at 99% on a live corridor — the moment a rider is most likely to decide it
        # has hung, and the last place left where the bar stops describing anything.
        yield work.step(ENRICH_STAGE, f"checking {len(found)} places are real")
        resolved = await self._resolve(found, leg, counts, concurrency)

        yield work.step(ENRICH_STAGE, f"{len(resolved)} are real and on the route")
        scored = await self._score(resolved, leg, counts)

        pois = tuple(_to_poi(item) for item in scored if item.resolved.category is not None)
        yield work.step(ENRICH_STAGE, f"scored {len(scored)}")

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
        self, query: SearchQuery, anchor: Coordinate, counts: _Counts
    ) -> list[Candidate]:
        try:
            results = await self._source.search(query, near=anchor)
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
        return resolved

    async def _score(
        self,
        resolved: Sequence[ResolvedCandidate],
        leg: RouteLeg,
        counts: _Counts,
    ) -> tuple[ScoredCandidate, ...]:
        if not resolved:
            return ()
        try:
            return await self._judge.judge(resolved, leg)
        except (DiscoveryError, LlmError) as exc:
            # Everything up to here is already paid for. Losing the scores is bad; losing
            # the run because scoring timed out is worse.
            counts.failures.append(f"judge: {exc}")
            return ()

    @staticmethod
    def _summary(counts: _Counts, pinned: int) -> str:
        parts = [
            f"{counts.searched} results",
            f"{counts.named} named",
            f"{counts.resolved} on the route",
            f"{pinned} worth showing",
        ]
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
