"""Putting a found place into the waypoint list where the rider would meet it.

`add_poi_to_route` appended. For one place that is survivable; for four it is nonsense, and
the version of this that routes through several at once makes it obvious — a cafe two
kilometres from the start becomes the destination and the trip goes to the mountains and
back for lunch.

Position along the route is arithmetic on geometry we already have, so this is a pure
function with no provider, no model and no I/O. It decides *where*, never *whether*: that
question belongs to the selector, which knows the scores.
"""

from collections.abc import Sequence

from motorooter.planning.metrics import position_along_m
from motorooter.routing.geo import haversine_m
from motorooter.routing.models import Coordinate
from motorooter.trips.models import Waypoint

SAME_PLACE_M = 60.0
"""Within this, two waypoints are the same stop and the second is dropped.

Not deduplication for tidiness — routing through the same spot twice produces a zero-length
leg, which every engine handles differently and none handles well. Sixty metres because the
same building reached via Places, via a map click and via a search result will not agree to
the metre, while two genuinely distinct places worth separate stops are further apart than
the width of a road.
"""


MINIMUM_FOR_A_MIDDLE = 2
"""Below two waypoints there is no middle to insert into, so an addition just goes last."""


def insert_in_route_order(
    waypoints: Sequence[Waypoint],
    additions: Sequence[Waypoint],
    *,
    geometry: Sequence[Coordinate],
) -> tuple[Waypoint, ...]:
    """`waypoints` with `additions` spliced in at the positions the route implies.

    Args:
        waypoints: the trip as it stands. Returned unchanged if there is nothing to add.
        additions: places to route through. Order is ignored — the route decides it.
        geometry: the routed line the positions are measured along.

    **The first and last waypoints stay first and last.** A discovered place is a via-point
    however well it scored; a rider who asked for camping and got a new destination has had
    their trip taken off them. So a place that projects beyond either end is clamped inside
    rather than allowed to become an endpoint.

    Without geometry the ordering is unknowable, and this appends before the last waypoint
    instead of guessing. That is the honest degradation: the endpoint invariant is still
    knowable and still enforced, and only the ordering among the middle is lost.
    """
    if not additions:
        return tuple(waypoints)

    placed = list(waypoints)
    for addition in additions:
        if any(_same_place(addition, existing) for existing in placed):
            # Already a stop on this trip — either the rider's own or one added a moment
            # ago in this same call, which is why `placed` and not `waypoints` is the thing
            # checked against. Adding it again is a zero-length leg, not a second visit.
            continue
        placed.insert(_index_for(addition, placed, geometry), addition)
    return tuple(placed)


def _index_for(
    addition: Waypoint, placed: Sequence[Waypoint], geometry: Sequence[Coordinate]
) -> int:
    """Where in `placed` this addition belongs, never displacing an endpoint."""
    if len(placed) < MINIMUM_FOR_A_MIDDLE:
        return len(placed)

    last_slot = len(placed) - 1
    position = position_along_m(geometry, addition.coordinate)
    if position is None:
        return last_slot

    for index in range(1, last_slot):
        existing = position_along_m(geometry, placed[index].coordinate)
        if existing is not None and existing > position:
            return index
    return last_slot


def _same_place(one: Waypoint, other: Waypoint) -> bool:
    return haversine_m(one.coordinate, other.coordinate) <= SAME_PLACE_M
