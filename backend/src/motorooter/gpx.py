"""GPX for a motorcycle GPS.

A track plus ordered waypoints, which is what the target devices actually consume. Discovered
POIs travel as waypoints because on the device that is most of the value: a line is
navigable, and a line with your campsite on it is a plan.

**Decimated, never truncated.** CLAUDE.md is explicit, and the reason is what truncation does
to a real route — a WABDR section is thousands of points, and cutting at the limit hands a
rider the first third of their day with nothing to say the rest is missing. Decimation keeps
the whole route and spends the budget on the corners.
"""

import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence

from motorooter.routing.models import Coordinate
from motorooter.trips.models import Trip

GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"
CREATOR = "MotoRooter"

GARMIN_TRACK_POINT_LIMIT = 10_000
"""Track points in one exported file.

Garmin's limits vary by unit and firmware, and the range is wide: older handhelds cut tracks
at 500 points per segment, while zūmo-class motorcycle units handle far more. 10,000 is the
figure most commonly documented for modern units and is the one to start from.

**This wants checking against Tim's actual device before it is trusted**, which is why GPX
sits where it does in the queue. It is a single constant precisely so that check changes one
number rather than an algorithm — and the decimation below is correct at any limit, so being
wrong here costs fidelity rather than correctness.
"""


def decimate(
    points: Sequence[Coordinate], *, limit: int = GARMIN_TRACK_POINT_LIMIT
) -> tuple[Coordinate, ...]:
    """At most `limit` points, keeping the shape.

    Ramer-Douglas-Peucker rather than every-nth, because a hairpin is the shape a rider
    cares about and even sampling is exactly as likely to drop it as any redundant point on
    a straight. The algorithm keeps whichever points are furthest from the line their
    neighbours describe, which is a description of a corner.

    Both endpoints always survive: a route that ends early is the truncation failure this
    exists to avoid, in a subtler form.

    Tolerance is found by bisection rather than chosen, since the requirement is a point
    budget rather than an accuracy. Twenty iterations resolve it to about one part in a
    million of the route's own extent, which is far finer than the coordinates justify.
    """
    if len(points) <= max(limit, 2):
        return tuple(points)

    span = _extent(points)
    if span <= 0:
        # Every point is in the same place. Nothing to simplify towards, so keep the ends.
        return (points[0], points[-1])

    low, high = 0.0, span
    best: tuple[Coordinate, ...] = (points[0], points[-1])
    for _ in range(20):
        middle = (low + high) / 2
        kept = _simplify(points, middle)
        if len(kept) <= limit:
            best = kept
            high = middle
        else:
            low = middle
    return best


def _extent(points: Sequence[Coordinate]) -> float:
    """Rough size of the bounding box, as the upper bound for the tolerance search."""
    lats = [point.lat for point in points]
    lons = [point.lon for point in points]
    return max(max(lats) - min(lats), max(lons) - min(lons))


def _simplify(points: Sequence[Coordinate], tolerance: float) -> tuple[Coordinate, ...]:
    """Ramer-Douglas-Peucker, iteratively.

    Iterative rather than recursive because the recursion depth is the point count, and a
    10,000-point WABDR track would exhaust the stack on the exact input this exists for.
    """
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        furthest, distance = _furthest_from_line(points, start, end)
        if distance > tolerance:
            keep[furthest] = True
            stack.append((start, furthest))
            stack.append((furthest, end))

    return tuple(point for point, kept in zip(points, keep, strict=True) if kept)


def _furthest_from_line(points: Sequence[Coordinate], start: int, end: int) -> tuple[int, float]:
    """The point between `start` and `end` furthest from the line joining them.

    Perpendicular distance in degrees, not metres. The comparison is between points on the
    same short span, so the latitude scaling that would matter over a continent cancels —
    and the tolerance is searched for rather than specified, so its units never surface.
    """
    first, last = points[start], points[end]
    dx = last.lon - first.lon
    dy = last.lat - first.lat
    scale = (dx * dx + dy * dy) ** 0.5

    furthest, worst = start + 1, -1.0
    for index in range(start + 1, end):
        point = points[index]
        if scale == 0:
            distance = ((point.lon - first.lon) ** 2 + (point.lat - first.lat) ** 2) ** 0.5
        else:
            distance = abs(dx * (first.lat - point.lat) - (first.lon - point.lon) * dy) / scale
        if distance > worst:
            furthest, worst = index, distance
    return furthest, worst


def trip_to_gpx(trip: Trip, *, limit: int = GARMIN_TRACK_POINT_LIMIT) -> str:
    """The trip as a GPX 1.1 document.

    One track with one segment per leg. One track because it is one ride and two would be two
    things to select on the device; one segment per leg because that is what a segment means
    — these points are contiguous, that gap is not — and joining them would draw a line
    across a gap the router never routed.
    """
    root = ElementTree.Element(f"{{{GPX_NAMESPACE}}}gpx", {"version": "1.1", "creator": CREATOR})
    metadata = ElementTree.SubElement(root, f"{{{GPX_NAMESPACE}}}metadata")
    ElementTree.SubElement(metadata, f"{{{GPX_NAMESPACE}}}name").text = trip.name

    for index, waypoint in enumerate(trip.waypoints):
        _waypoint(root, waypoint.coordinate, waypoint.name or f"Waypoint {index + 1}")
    for point_of_interest in trip.pois:
        _waypoint(
            root,
            point_of_interest.coordinate,
            point_of_interest.name,
            kind=point_of_interest.category.value,
            note=point_of_interest.note,
        )

    track = ElementTree.SubElement(root, f"{{{GPX_NAMESPACE}}}trk")
    ElementTree.SubElement(track, f"{{{GPX_NAMESPACE}}}name").text = trip.name
    for geometry in _decimated_legs(trip, limit):
        segment = ElementTree.SubElement(track, f"{{{GPX_NAMESPACE}}}trkseg")
        for point in geometry:
            ElementTree.SubElement(
                segment,
                f"{{{GPX_NAMESPACE}}}trkpt",
                {"lat": f"{point.lat:.6f}", "lon": f"{point.lon:.6f}"},
            )

    ElementTree.indent(root)
    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True)


def _decimated_legs(trip: Trip, limit: int) -> list[tuple[Coordinate, ...]]:
    """Each routed leg's geometry, sharing one budget across the trip.

    Shared because the limit is a property of the file, not of a leg: per-leg budgets would
    let a ten-leg trip ship ten times the limit and hit exactly the device ceiling this
    exists to respect. Divided by length, so a 200 km leg keeps more points than a 5 km one
    rather than each getting an equal share of a budget they do not equally need.
    """
    geometries = [leg.routed.geometry for leg in trip.legs if leg.routed is not None]
    total = sum(len(geometry) for geometry in geometries)
    if total <= limit:
        return [tuple(geometry) for geometry in geometries]

    return [
        decimate(geometry, limit=max(int(limit * len(geometry) / total), 2))
        for geometry in geometries
    ]


def _waypoint(
    root: ElementTree.Element,
    coordinate: Coordinate,
    name: str,
    *,
    kind: str | None = None,
    note: str | None = None,
) -> None:
    element = ElementTree.SubElement(
        root,
        f"{{{GPX_NAMESPACE}}}wpt",
        {"lat": f"{coordinate.lat:.6f}", "lon": f"{coordinate.lon:.6f}"},
    )
    ElementTree.SubElement(element, f"{{{GPX_NAMESPACE}}}name").text = name
    if note:
        # The judge's reason, which is the only thing on the device that says *why* this
        # place is on the list.
        ElementTree.SubElement(element, f"{{{GPX_NAMESPACE}}}desc").text = note
    if kind:
        ElementTree.SubElement(element, f"{{{GPX_NAMESPACE}}}type").text = kind


__all__ = ["GARMIN_TRACK_POINT_LIMIT", "GPX_NAMESPACE", "decimate", "trip_to_gpx"]
