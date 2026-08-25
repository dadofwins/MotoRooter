"""Numbers about a route, computed from its geometry.

The discovery scorer is meant to be *handed* these rather than to estimate them. A model
asked "how twisty is this road" answers confidently and unfalsifiably; a model handed "412
degrees of heading change per kilometre" is being asked the question it is actually good at
— whether that is worth riding — and the number is testable like any other function.

Everything here is pure and synchronous. No provider, no I/O, no model.

**Not here, and worth knowing why: elevation gain.** `Coordinate` carries no elevation —
the ORS adapter drops the third ordinate at the boundary — so it cannot be computed from
geometry today. `RouteLeg.ascent_m` holds the provider's figure, and that figure is
currently suspect: 6,400-8,800 m reported against a 3,188 m reference on the WABDR spike.
Inventing a derived climb number on top of that would be worse than having none.
"""

from collections.abc import Sequence
from itertools import pairwise
from math import atan2, cos, degrees, radians, sin, sqrt

from motorooter.routing.geo import EARTH_RADIUS_M, haversine_m, path_length_m
from motorooter.routing.models import Coordinate

MIN_HEADING_SEGMENT_M = 50.0
"""Shortest segment whose bearing is trusted when measuring heading change.

Route geometry is densely sampled and slightly jittery, so a straight highway is not a
straight line — it is thousands of nearly-collinear points a metre or two apart. The bearing
between two such points is dominated by rounding, and summing those bearings measures the
sampling rather than the road: a dead-straight motorway can score higher than a mountain
pass. Ignoring short hops measures the shape instead.

Fifty metres, because the threshold has to exceed the *sampling interval* to do anything at
all — at 15 m it never engaged on geometry sampled every 20 m, and half a metre of wobble
across those hops still scored a dead-straight road at 29 deg/km. Aggregating to longer
segments costs almost no signal: total heading change through a corner is the same whether
it is measured in one step or ten, so a hairpin still reads as a hairpin.

Like the other tuning constants here it is reasoned rather than measured, and it should be
checked against a real BDR trace before anything is judged on it.
"""


def bearing_deg(start: Coordinate, end: Coordinate) -> float:
    """Initial great-circle bearing from `start` to `end`, in degrees clockwise from north."""
    lat1, lat2 = radians(start.lat), radians(end.lat)
    dlon = radians(end.lon - start.lon)
    y = sin(dlon) * cos(lat2)
    x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return degrees(atan2(y, x)) % 360.0


def _turn_deg(first: float, second: float) -> float:
    """Absolute change between two bearings, in [0, 180].

    Wrapped, because 359° to 1° is a two-degree turn and not a 358-degree one — the mistake
    that makes any route crossing due north look like a series of hairpins.

    Absolute, because signed change cancels: an S-bend is two opposite turns and would
    report as perfectly straight, which is the opposite of the truth.
    """
    delta = abs(second - first) % 360.0
    return 360.0 - delta if delta > 180.0 else delta


def twistiness_deg_per_km(
    geometry: Sequence[Coordinate], *, min_segment_m: float = MIN_HEADING_SEGMENT_M
) -> float:
    """Total heading change per kilometre.

    Per kilometre rather than in total, so a long straight road does not outscore a short
    mountain pass simply by being long. Independent of sampling density, because it measures
    the accumulated turn of the line rather than the number of vertices in it.

    Degenerate geometry scores zero rather than raising: an unrouted or zero-length leg is a
    legitimate state, not an error.
    """
    bearings: list[float] = []
    previous = None
    for point in geometry:
        if previous is None:
            previous = point
            continue
        if haversine_m(previous, point) < min_segment_m:
            # Too short to have a trustworthy heading. Skipped without advancing `previous`,
            # so a run of tiny hops accumulates into one segment long enough to measure
            # rather than being discarded.
            continue
        bearings.append(bearing_deg(previous, point))
        previous = point

    length_km = path_length_m(geometry) / 1000.0
    if length_km <= 0 or len(bearings) < 2:
        return 0.0

    total_turn = sum(_turn_deg(first, second) for first, second in pairwise(bearings))
    return total_turn / length_km


def detour_ratio(geometry: Sequence[Coordinate]) -> float:
    """Route length divided by the straight line between its endpoints.

    1.0 is a road that goes directly there. Higher means it wanders — which for this app is
    usually the point, so it is evidence rather than a penalty. A loop returning to its start
    has no meaningful direct line, so it reports its own length in kilometres rather than
    dividing by something near zero.
    """
    if len(geometry) < 2:
        return 1.0

    travelled = path_length_m(geometry)
    if travelled <= 0:
        return 1.0

    direct = haversine_m(geometry[0], geometry[-1])
    if direct < MIN_HEADING_SEGMENT_M:
        # Effectively a loop. Dividing here produces an arbitrarily large number driven by
        # how precisely the route closed, which is noise rather than a property of the ride.
        return travelled / 1000.0
    return travelled / direct


def nearest_distance_m(geometry: Sequence[Coordinate], point: Coordinate) -> float | None:
    """Shortest distance from `point` to the route, or `None` if there is no route.

    Measured to the *segments*, not only to the vertices: a candidate beside the middle of a
    long straight is next to the road, and vertex-only distance would call it kilometres
    away and reject a detour of nothing.

    This is the input to "is the detour worth it", not the answer to it — the true cost of
    including a point is the extra distance the route grows by, which depends on where it is
    inserted. Roughly twice this, and worth computing properly once insertion exists.
    """
    if not geometry:
        return None
    if len(geometry) == 1:
        return haversine_m(geometry[0], point)

    return min(_distance_to_segment_m(start, end, point) for start, end in pairwise(geometry))


def _distance_to_segment_m(start: Coordinate, end: Coordinate, point: Coordinate) -> float:
    """Point-to-segment distance, on a local flat approximation.

    Equirectangular rather than spherical: segments of route geometry are metres to
    hundreds of metres long, where the curvature error is far below the precision anything
    downstream needs, and the projected form has a closed-form answer instead of an
    iterative one.
    """
    scale = cos(radians(point.lat))
    origin_lat, origin_lon = start.lat, start.lon

    def project(coordinate: Coordinate) -> tuple[float, float]:
        x = radians(coordinate.lon - origin_lon) * scale * EARTH_RADIUS_M
        y = radians(coordinate.lat - origin_lat) * EARTH_RADIUS_M
        return x, y

    ax, ay = project(start)
    bx, by = project(end)
    px, py = project(point)

    dx, dy = bx - ax, by - ay
    squared = dx * dx + dy * dy
    if squared == 0:
        # Degenerate segment; fall back to the endpoint.
        return sqrt((px - ax) ** 2 + (py - ay) ** 2)

    # Clamped, so a point beyond either end measures to that end rather than to an
    # imaginary extension of the road.
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / squared))
    return sqrt((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2)
