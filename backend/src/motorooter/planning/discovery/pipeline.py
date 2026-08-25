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

**One implementation, two callers.** The REST endpoint and the assistant's tools both run
this. Item 5 of M1 — route through the found POIs — is reachable by button and by chat, and
two implementations of it would diverge silently, since both would produce something
plausible.

Nothing here raises for an ordinary failure. A corridor where one category finds nothing, or
one search times out, should cost those results rather than the run: the rest of it has
already been paid for in metered requests.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from motorooter.planning.discovery.category import CategoryClassifier
from motorooter.planning.discovery.corridor import anchors
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
from motorooter.planning.discovery.queries import queries_for
from motorooter.planning.discovery.resolve import PlacesResolver
from motorooter.routing.models import RouteLeg
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
        max_anchors: int = 6,
    ) -> AsyncIterator[DiscoveryProgress]:
        """Discover POIs along a routed leg.

        Args:
            leg: the routed geometry to search along.
            categories: what to look for. Anchors times categories is the metered cost.
            max_anchors: ceiling on search points, since the fan-out multiplies.
        """
        placed = anchors(leg.geometry, max_anchors=max_anchors)
        if not placed:
            yield DiscoveryProgress(stage=DONE_STAGE, message="nothing to search: no route")
            return

        counts = _Counts()
        candidates: list[Candidate] = []

        for index, anchor in enumerate(placed):
            progress = (index + 1) / (len(placed) + 1)
            name, region = await self._describe(anchor, counts)
            if name is None:
                yield DiscoveryProgress(
                    stage=SEARCH_STAGE,
                    message=f"nothing named near point {index + 1}",
                    progress=progress,
                )
                continue

            found = await self._search_around(anchor, name, region, categories, counts)
            candidates.extend(found)
            yield DiscoveryProgress(
                stage=SEARCH_STAGE,
                message=f"searched near {name}: {len(found)} leads",
                progress=progress,
            )

        yield DiscoveryProgress(
            stage=ENRICH_STAGE,
            message=f"checking {len(candidates)} leads against Places",
            progress=0.8,
        )

        scored = await self._enrich(candidates, leg, counts)
        pois = tuple(_to_poi(item) for item in scored if item.resolved.category is not None)

        yield DiscoveryProgress(
            stage=DONE_STAGE,
            message=self._summary(counts, len(pois)),
            progress=1.0,
            pois=pois,
            scored=scored,
        )

    async def _describe(self, anchor: object, counts: _Counts) -> tuple[str | None, str | None]:
        try:
            return (
                await self._namer.name_for(anchor),  # type: ignore[arg-type]
                await self._namer.region_for(anchor),  # type: ignore[arg-type]
            )
        except DiscoveryError as exc:
            # One unnameable anchor is a gap in the corridor, not a failed run.
            counts.failures.append(f"naming: {exc}")
            return None, None

    async def _search_around(
        self,
        anchor: object,
        name: str,
        region: str | None,
        categories: Sequence[PoiCategory],
        counts: _Counts,
    ) -> list[Candidate]:
        named: list[Candidate] = []
        for query in queries_for(name, categories):
            try:
                results = await self._source.search(query, near=anchor)  # type: ignore[arg-type]
            except DiscoveryError as exc:
                counts.failures.append(f"search {query.category.value}: {exc}")
                continue
            counts.searched += len(results)
            extracted = await self._extractor.extract(results, region=region, searched_for=name)
            counts.named += len(extracted)
            named.extend(extracted)
        return named

    async def _enrich(
        self, candidates: Sequence[Candidate], leg: RouteLeg, counts: _Counts
    ) -> tuple[ScoredCandidate, ...]:
        if not candidates:
            return ()
        try:
            resolved = await self._resolver.resolve(candidates, route=leg.geometry)
            resolved = await self._classifier.classify(resolved)
        except DiscoveryError as exc:
            counts.failures.append(f"resolve: {exc}")
            return ()

        resolved = _unique(resolved)
        counts.resolved = len(resolved)
        return await self._judge.judge(resolved, leg)

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
