"""Turning roads into searches.

A road is a lead rather than a result. `Route 410` scoring 0.95 was the pipeline being right
about what is interesting and then stopping one step short — a rider cannot pull over at a
scenic byway, they ride it. But "this road is worth riding" is precisely the signal that
should produce "and here is the viewpoint on it worth stopping for".

The endpoint's first live run made the case better than the argument did: the only two
candidates that survived resolution were WA-410 and WA-123, so the pipeline was confidently
identifying the best things on the route and discarding both.

**Capped at one hop.** Expansion is a real fan-out multiplier — roads times queries times
anchors — and a road found by expanding a road would walk the highway network, since every
road mentions others.
"""

from collections.abc import Iterable, Sequence

from motorooter.planning.discovery.grounding import normalize
from motorooter.planning.discovery.models import Candidate, CandidateKind
from motorooter.planning.discovery.queries import SearchQuery
from motorooter.trips.models import PoiCategory

ROAD_QUERY_TEMPLATES: tuple[tuple[str, PoiCategory], ...] = (
    ("best viewpoints and pull-offs on {road}", PoiCategory.VIEWPOINT),
    ("where to stop on {road} motorcycle", PoiCategory.VIEWPOINT),
)
"""What to ask about a road worth riding.

Deliberately few. Each one multiplies by every road on every anchor, and two well-aimed
queries beat six vague ones — the road is already known to be good, so the question is only
what is on it.
"""


def roads_and_places(
    candidates: Iterable[Candidate],
) -> tuple[tuple[Candidate, ...], tuple[Candidate, ...]]:
    """Split candidates into results and leads.

    Roads already reached by an expansion are excluded from the leads: that is the one-hop
    cap, and it is enforced here rather than by a counter, so it cannot be lost by a caller
    forgetting to pass one.

    Roads are deduplicated by normalised name — two anchors naming the same byway should not
    pay for it twice.
    """
    places: list[Candidate] = []
    roads: list[Candidate] = []
    seen: set[str] = set()

    for candidate in candidates:
        if candidate.kind is not CandidateKind.ROAD:
            places.append(candidate)
            continue
        if candidate.found_via is not None:
            continue
        key = normalize(candidate.name)
        if key in seen:
            continue
        seen.add(key)
        roads.append(candidate)

    return tuple(places), tuple(roads)


def expansion_queries(road: Candidate) -> tuple[SearchQuery, ...]:
    """Searches for what is worth stopping at along a road.

    Empty for anything that is not a road, so a caller can pass everything without filtering
    twice.
    """
    if road.kind is not CandidateKind.ROAD:
        return ()
    return tuple(
        SearchQuery(text=template.format(road=road.name), category=category, place=road.name)
        for template, category in ROAD_QUERY_TEMPLATES
    )


def attribute(found: Sequence[Candidate], road: Candidate) -> tuple[Candidate, ...]:
    """Record which road surfaced each of these.

    Carried into scoring, and it is the whole reason expansion is worth more than another
    search: a viewpoint on a road people ride for pleasure is worth more than the same
    viewpoint on a road nobody mentions, and nothing else in the evidence says so.
    """
    return tuple(
        candidate.model_copy(update={"found_via": road.name, "kind": CandidateKind.PLACE})
        for candidate in found
    )
