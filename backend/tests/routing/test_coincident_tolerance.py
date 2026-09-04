"""One value for "these two points are the same place".

It was briefly two: `planning.stitching` had one for a leg boundary and the span guard had
another for a zero-length request. Same meaning, same number, two homes — so the day somebody
tuned one, the other would silently disagree, and a boundary would behave differently from a
span with nothing anywhere to say why.

`routing.geo` is the home because the dependency already runs that way: `planning` imports
from `routing`, and nothing under `routing/` imports from `planning` at all. So this is not
an inversion; the constant moved to the layer both callers already depended on.

A drift tripwire rather than a behaviour check. It asserts the two callers still *act* on the
shared value, so re-introducing a local copy fails here the moment its value differs — which
is the only way this regresses, and the way that is otherwise invisible.
"""

import inspect

from motorooter.planning.stitching import stitch
from motorooter.planning.trip_router import TripRouter
from motorooter.routing.decorators.coincident import CoincidentSpanProvider
from motorooter.routing.geo import COINCIDENT_TOLERANCE_M
from motorooter.routing.models import Coordinate, LegIntent, RouteRequest
from motorooter.routing.providers.fake import FakeProvider


def default_tolerance_of(function) -> float:
    parameter = inspect.signature(function).parameters["coincident_tolerance_m"]
    assert isinstance(parameter.default, float)
    return parameter.default


def test_stitching_collapses_boundaries_at_the_shared_tolerance():
    assert default_tolerance_of(stitch) == COINCIDENT_TOLERANCE_M


def test_both_stitching_entry_points_on_the_trip_router_use_the_same_one():
    assert default_tolerance_of(TripRouter.stitch_result) == COINCIDENT_TOLERANCE_M
    assert default_tolerance_of(TripRouter.stitch_trip) == COINCIDENT_TOLERANCE_M


async def test_the_span_guard_short_circuits_at_the_shared_tolerance():
    """Just inside it is one point; just outside is a route worth asking an engine for."""
    point = (47.5962, -120.6615)
    metres_per_degree_lat = 111_320.0

    def span(offset_m: float) -> RouteRequest:
        moved = Coordinate(lat=point[0] + offset_m / metres_per_degree_lat, lon=point[1])
        return RouteRequest(
            waypoints=(Coordinate(lat=point[0], lon=point[1]), moved),
            intent=LegIntent.UNPAVED,
        )

    inside = FakeProvider()
    await CoincidentSpanProvider(inside).route(span(COINCIDENT_TOLERANCE_M * 0.5))
    assert inside.call_count == 0

    outside = FakeProvider()
    await CoincidentSpanProvider(outside).route(span(COINCIDENT_TOLERANCE_M * 2))
    assert outside.call_count == 1
