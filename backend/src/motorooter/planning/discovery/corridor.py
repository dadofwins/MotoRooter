"""Reducing a route to the points discovery searches around.

Nothing discovery talks to can search "along a route" — Brave and Places both take a place.
So the route becomes a series of anchors, and every later stage fans out from them.

The spacing is load-bearing twice. It is the search budget: anchors multiplied by categories
is the number of metered requests a corridor costs. And it is the same quantity M0 identified
as a hard *routing* requirement — endpoint-only routing produced a plausible route down the
wrong roads, 37% of it near the real BDR, while eight intermediate waypoints put 58% there.

One function for both, deliberately. A separate implementation for routing would drift from
the one discovery uses, and the drift would be invisible: both produce continuous geometry,
and only a comparison against a route nobody has would show the difference.
"""

from collections.abc import Sequence
from itertools import pairwise
from typing import Protocol

from motorooter.routing.geo import haversine_m, path_length_m
from motorooter.routing.models import Coordinate, SurfaceSpan


class SearchCorridor(Protocol):
    """The stretch of road discovery searches, as narrowly as discovery actually reads it.

    Two attributes, because those are the two the stages use: anchors and the distance filter
    read `geometry`, and the judge's evidence reads `surface_spans` around each candidate.
    Nothing here wants a distance, a duration, a provider or an intent.

    A protocol rather than a `RouteLeg` because a trip's corridor is several legs joined, and
    the honest join is a `StitchedRoute`. Fabricating a `RouteLeg` to carry it would have to
    invent a provider and an intent for a route that has several of each — a plausible object
    that is wrong in exactly the way this codebase keeps catching. Both types satisfy this as
    they are.
    """

    @property
    def geometry(self) -> tuple[Coordinate, ...]: ...

    @property
    def surface_spans(self) -> tuple[SurfaceSpan, ...]: ...


DEFAULT_ANCHOR_SPACING_M = 12_500.0
"""Distance between anchors, in the 10-15 km band M0 measured.

Mid-band rather than either edge: 15 km is the loosest spacing that reproduced a BDR, and
10 km costs 50% more searches for a route that was already good enough at 15. This sits
where a miss in either direction is small.
"""

_COINCIDENT_M = 1.0
"""Below this, an interpolated anchor and the route end are the same place."""

DISCOVERY_ANCHOR_SPACING_M = 25_000.0
"""Distance between anchors when searching for places, as opposed to routing.

Deliberately coarser than `DEFAULT_ANCHOR_SPACING_M`, because it answers a different
question. M0's 12.5 km is the density needed to *reproduce a known route* — below it the
engine finds different roads. Discovery is not reproducing anything: a rider does not need a
fresh search every 12.5 km, and at that spacing a 300 km trip is 24 anchors and 216 searches
for one button press.

Twice the corridor half-width is the natural floor. Places are kept within 15 km of the
route, so anchors closer together than about 25 km search overlapping ground and return the
same campsite twice — which is exactly what the live runs showed before deduplication was
added. Spacing them at the width of what they cover is the point where extra searches start
buying new places rather than repeats.
"""

DEFAULT_MAX_ANCHORS = 40
"""Ceiling on anchors per route, whatever its length.

Anchor count is the fan-out, and both Brave and Places are metered. A thousand-kilometre trip
at 12.5 km would be eighty anchors before multiplying by categories, which is the kind of
number that turns into an opaque rate-limit failure rather than a search.
"""


def anchors(
    geometry: Sequence[Coordinate],
    *,
    spacing_m: float = DEFAULT_ANCHOR_SPACING_M,
    max_anchors: int = DEFAULT_MAX_ANCHORS,
) -> tuple[Coordinate, ...]:
    """Points along the route, roughly `spacing_m` apart, always including both ends.

    Both ends always, because a rider's first and last stops matter as much as the middle,
    and a route whose end is never searched has a silent hole at exactly the point someone
    is looking for a bed.

    If the requested spacing would exceed `max_anchors`, the spacing *widens* rather than the
    route being truncated. Dropping the tail would stop searching the back half of a trip
    while still reporting success, which is the worse failure of the two.

    Raises:
        ValueError: spacing is not positive, which would place anchors without limit.
    """
    if spacing_m <= 0:
        msg = f"spacing_m must be positive, got {spacing_m}"
        raise ValueError(msg)
    if max_anchors < 2:
        # Both ends are always anchors, so one is not a possible answer — and the widening
        # arithmetic below divides by `max_anchors - 1`. Found by a test passing 1.
        msg = f"max_anchors must be at least 2 (both route ends), got {max_anchors}"
        raise ValueError(msg)
    if len(geometry) < 2:
        return tuple(geometry)

    total = path_length_m(geometry)
    if total <= 0:
        # A zero-length route: both ends are the same point, so one anchor says everything.
        return (geometry[0],)

    # Widen rather than truncate. `max_anchors - 1` intervals span the whole route.
    spacing_m = max(spacing_m, total / (max_anchors - 1))

    placed = [geometry[0]]
    travelled = 0.0
    next_at = spacing_m

    for start, end in pairwise(geometry):
        segment = haversine_m(start, end)
        if segment <= 0:
            continue
        # Leave room for the route end, which is appended unconditionally below. Without
        # the bound, an anchor landing a floating-point hair short of the end is followed by
        # the end itself and the cap is exceeded by one.
        while travelled + segment >= next_at and len(placed) < max_anchors - 1:
            placed.append(_interpolate(start, end, (next_at - travelled) / segment))
            next_at += spacing_m
        travelled += segment

    if haversine_m(placed[-1], geometry[-1]) > _COINCIDENT_M:
        placed.append(geometry[-1])
    else:
        # Snap the final anchor onto the actual endpoint rather than leaving it a metre
        # short: the end of the route is a place someone will look for a bed.
        placed[-1] = geometry[-1]
    return tuple(placed)


def spacing_of(placed: Sequence[Coordinate]) -> float:
    """Mean gap between anchors, so a tool's output can be asserted on rather than assumed.

    The M0 requirement is a property of what a tool *emits*, not of what it intended, and a
    density that silently degrades produces a plausible route down the wrong roads.
    """
    if len(placed) < 2:
        return 0.0
    return path_length_m(placed) / (len(placed) - 1)


def _interpolate(start: Coordinate, end: Coordinate, fraction: float) -> Coordinate:
    """A point `fraction` of the way along a segment.

    Linear in degrees. Over segments of route geometry — metres to hundreds of metres — the
    difference from a great-circle interpolation is far below anything that matters to a
    search query centred on the result.
    """
    return Coordinate(
        lat=start.lat + (end.lat - start.lat) * fraction,
        lon=start.lon + (end.lon - start.lon) * fraction,
    )
