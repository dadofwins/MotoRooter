"""What the trip says about itself, computed rather than asked.

Measure what is measurable; ask the model only what is not — the same split as the discovery
judge, for the same reason. Everything here is arithmetic over a document we already hold, so
a model would be slower, non-deterministic, and capable of being confidently wrong about a
number it could have been handed.

It is also what makes "never state a number or a place you were not given" enforceable
rather than hoped for: the prompt is built from these fields alone, so a figure that is not
here is one the model had no way to learn.

`None` throughout means "not known yet", never zero. A trip that has not been routed has no
surface shares; reporting 0% dirt would be a measurement, and a wrong one.
"""

import dataclasses
from collections import Counter
from collections.abc import Mapping

from motorooter.routing.geo import haversine_m
from motorooter.routing.models import RIDER_FACING_MODE, RouteLeg
from motorooter.trips.models import Trip

LOOP_TOLERANCE_M = 100.0
"""How close the last waypoint must be to the first for the trip to be a loop.

Looser than the routing layer's coincidence tolerance on purpose: that one asks "are these
the same point", which is a question about float jitter, and this asks "does the rider end up
back where they started", which is a question about a town. Two ends of a Leavenworth loop
pinned at opposite ends of the main street are a loop to everyone except a metre count.
"""

MAX_NAMED_PLACES = 5
"""Places named in the prompt. Enough to be specific, few enough not to become a list."""


@dataclasses.dataclass(frozen=True)
class TripFacts:
    """The evidence the blurb is allowed to draw on. Every field is measured or absent."""

    waypoint_names: tuple[str, ...] = ()
    leg_count: int = 0
    is_loop: bool = False

    distance_km: float | None = None
    unpaved_share: float | None = None
    paved_share: float | None = None
    unsurveyed_share: float | None = None

    riding_modes: tuple[str, ...] = ()
    place_counts: Mapping[str, int] = dataclasses.field(default_factory=dict)
    named_places: tuple[tuple[str, str], ...] = ()
    """Each name with its own category, never a bare list of names.

    Paired because unpairing them shipped a defect: given the counts and the names as two
    separate lists, the model had no way to know which name was the food one and said
    "grab grub at Halfway Flat", a wild camp. The join is arithmetic we already hold, so
    withholding it was asking the model to invent a fact — which is the one thing the
    prompt forbids, and not something a prompt can forbid when the format invites it.
    """

    @property
    def is_routed(self) -> bool:
        return self.distance_km is not None


def facts_for(trip: Trip) -> TripFacts:
    """Everything measurable about a trip, for the blurb prompt."""
    routed = tuple(leg.routed for leg in trip.legs if leg.routed is not None)
    distance_km, shares = _distance_and_shares(routed)
    unpaved, paved, unsurveyed = shares
    counts = Counter(poi.category.value for poi in trip.pois)

    return TripFacts(
        # Unnamed waypoints are dropped rather than labelled: a name is a term the model may
        # repeat to the rider, and "unnamed point" is not one.
        waypoint_names=tuple(point.name for point in trip.waypoints if point.name),
        leg_count=len(trip.legs),
        is_loop=_is_loop(trip),
        distance_km=distance_km,
        unpaved_share=unpaved,
        paved_share=paved,
        unsurveyed_share=unsurveyed,
        riding_modes=_modes(trip),
        place_counts=dict(counts),
        named_places=tuple((poi.name, poi.category.value) for poi in trip.pois[:MAX_NAMED_PLACES]),
    )


def _distance_and_shares(
    routed: tuple[RouteLeg, ...],
) -> tuple[float | None, tuple[float | None, float | None, float | None]]:
    """Distance in km and the three surface shares, or all `None` if nothing is routed.

    The domain computes the three distances, including `unknown` as the remainder rather
    than the sum of UNKNOWN spans — geometry no span covers is exactly as unsurveyed as
    geometry tagged unsurveyed. Recomputing that by hand got it wrong once already.
    """
    if not routed:
        return None, (None, None, None)

    total = sum(leg.geometry_length_m for leg in routed)
    if total <= 0:
        return None, (None, None, None)

    unpaved = sum(leg.unpaved_distance_m for leg in routed)
    paved = sum(leg.paved_distance_m for leg in routed)
    unknown = sum(leg.unknown_distance_m for leg in routed)
    return total / 1000.0, (unpaved / total, paved / total, unknown / total)


def _is_loop(trip: Trip) -> bool:
    """Whether the rider ends where they started — the most characterising single fact."""
    if len(trip.waypoints) < 2:
        return False
    first, last = trip.waypoints[0].coordinate, trip.waypoints[-1].coordinate
    return haversine_m(first, last) <= LOOP_TOLERANCE_M


def _modes(trip: Trip) -> tuple[str, ...]:
    """Rider-facing mode names actually in use, in the order the rider meets them.

    Falls back to the trip default when there are no legs, because a trip stripped back to
    one waypoint still knows what the rider asked for — the same reason `default_intent`
    exists. Intents with no rider-facing name contribute nothing rather than a guess.
    """
    intents = [leg.intent for leg in trip.legs]
    if not intents and trip.default_intent is not None:
        intents = [trip.default_intent]

    seen: list[str] = []
    for intent in intents:
        label = RIDER_FACING_MODE.get(intent)
        if label is not None and label not in seen:
            seen.append(label)
    return tuple(seen)
