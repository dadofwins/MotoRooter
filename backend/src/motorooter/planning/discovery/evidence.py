"""Assembling the numbers the judge is handed.

The rule this stage exists to honour: **measure what is measurable, ask the model only what
is not.** Everything here is arithmetic on geometry already held — no provider call, no
model — so it is deterministic, cheap, and testable like any other function. A model asked
for these would be slower, non-deterministic, and perfectly capable of being confidently
wrong about a number it could have been given.

The local part is what makes it useful. "How twisty is the route" is a trip statistic; "what
is the road like *where this campsite is*" is what decides whether stopping there is any
good, and on a route that is half motorway and half mountain pass those are very different
numbers.

Ratings pass through here too. A rating is a fact about a place rather than a judgement about
it, so the model is handed it — and it is handed it in memory only, since Google's terms let
us keep `place_id` and almost nothing else.
"""

from collections.abc import Sequence

from motorooter.planning.discovery.models import Evidence, ResolvedCandidate
from motorooter.planning.metrics import nearest_distance_m, twistiness_deg_per_km
from motorooter.routing.geo import haversine_m, path_length_m
from motorooter.routing.models import Coordinate, RouteLeg, Surface
from motorooter.trips.models import PoiCategory

DEFAULT_WINDOW_M = 5_000.0
"""How much road around a place counts as "near" it when describing the riding.

Five kilometres is a couple of minutes either side — enough to characterise the road you
arrive on and leave by, short enough that a motorway ten minutes away does not colour a
mountain campsite. A guess, and an argument rather than a constant.
"""


def route_window(
    leg: RouteLeg, point: Coordinate, *, radius_m: float = DEFAULT_WINDOW_M
) -> tuple[Coordinate, ...]:
    """The stretch of route within `radius_m` of the nearest point to `point`.

    Never empty for a real route: a place far from the road still has a nearest stretch, and
    that stretch is the road someone would actually ride to reach it.
    """
    geometry = leg.geometry
    if len(geometry) < 2:
        return ()

    nearest = min(range(len(geometry)), key=lambda index: haversine_m(geometry[index], point))
    start = end = nearest
    while start > 0 and haversine_m(geometry[start - 1], geometry[nearest]) <= radius_m:
        start -= 1
    while end < len(geometry) - 1 and haversine_m(geometry[end + 1], geometry[nearest]) <= radius_m:
        end += 1

    # At least a segment, so twistiness has something to measure even at a tight radius.
    if start == end:
        start = max(0, nearest - 1)
        end = min(len(geometry) - 1, nearest + 1)
    return geometry[start : end + 1]


def assemble(
    resolved: ResolvedCandidate,
    leg: RouteLeg,
    *,
    others: Sequence[ResolvedCandidate] = (),
    window_m: float = DEFAULT_WINDOW_M,
) -> Evidence:
    """Measured evidence for one candidate.

    Args:
        resolved: the place being judged.
        leg: the routed leg it sits beside, carrying geometry and surface spans.
        others: everything else resolved on this corridor, for remoteness.
        window_m: how much road either side counts as local.

    Absent signals stay `None`. A missing surface reading is not a road with no dirt on it,
    and no fuel found is not fuel at zero metres — a scorer reading either as a number would
    rank an unmeasured road as flat tarmac with a filling station on it.
    """
    window = route_window(leg, resolved.coordinate, radius_m=window_m)
    _, unpaved, unknown = _surface_shares(leg, window)

    return Evidence(
        distance_off_route_m=resolved.distance_off_route_m,
        twistiness_deg_per_km=twistiness_deg_per_km(window) if len(window) > 1 else None,
        unpaved_fraction=unpaved,
        unknown_surface_fraction=unknown,
        distance_to_fuel_m=_distance_to_fuel(resolved, others),
        rating=resolved.rating,
        user_rating_count=resolved.user_rating_count,
    )


def _surface_shares(
    leg: RouteLeg, window: Sequence[Coordinate]
) -> tuple[float | None, float | None, float | None]:
    """Paved, unpaved and unknown shares of the windowed stretch.

    Three states, as everywhere else: geometry no span covers is unknown rather than paved,
    because an unsurveyed forest road is not a road that was surveyed and found to be tarmac.
    """
    if len(window) < 2:
        return None, None, None

    total = path_length_m(window)
    if total <= 0:
        return None, None, None

    # Locate the window inside the leg so the leg's spans can be read against it.
    offset = _window_offset(leg.geometry, window)
    if offset is None:
        return None, None, None

    covered = {Surface.PAVED: 0.0, Surface.UNPAVED: 0.0}
    last = offset + len(window) - 1
    for span in leg.surface_spans:
        if span.surface not in covered:
            continue
        start = max(span.start_index, offset)
        end = min(span.end_index, last)
        if end > start:
            covered[span.surface] += path_length_m(leg.geometry[start : end + 1])

    paved = min(covered[Surface.PAVED] / total, 1.0)
    unpaved = min(covered[Surface.UNPAVED] / total, 1.0)
    return paved, unpaved, max(0.0, 1.0 - paved - unpaved)


def _window_offset(geometry: Sequence[Coordinate], window: Sequence[Coordinate]) -> int | None:
    """Where the window starts in the leg. Identity-based, since the window is a slice."""
    first = window[0]
    for index, point in enumerate(geometry):
        if point is first:
            return index
    return None


def _distance_to_fuel(
    resolved: ResolvedCandidate, others: Sequence[ResolvedCandidate]
) -> float | None:
    """Straight-line distance to the nearest fuel stop, or `None` if none was found.

    `None` rather than zero, and rather than a large number: no fuel *discovered* is not the
    same as no fuel *existing*, and a scorer should treat it as a missing signal rather than
    as evidence of remoteness that may simply be a gap in the search.
    """
    stations = [
        other.coordinate
        for other in others
        if other.candidate.category is PoiCategory.FUEL and other is not resolved
    ]
    if not stations:
        return None
    return min(haversine_m(resolved.coordinate, station) for station in stations)


__all__ = ["DEFAULT_WINDOW_M", "assemble", "nearest_distance_m", "route_window"]
