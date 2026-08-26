"""Routing a trip through the best of what discovery found.

One service function behind two callers — the button and the assistant's tool — because this
is the case the mouse-path rule exists for. "Route me through the good ones" is a capability,
not a chat feature, and a chat-only version of it would be the inversion this project
forbids.

It is also an **autonomous edit to something the rider built**, which shapes everything here:

- It only ever adds *via*-points. `insert_in_route_order` refuses to displace the first or
  last waypoint, so a rider who asked for camping cannot end up with a new destination.
- It reports what it added and what it left out, so the reply can say plainly what happened
  rather than leaving the rider to spot a changed line on a map.
- It confirms the new route joins before writing anything. A place that verified against
  Places but sits somewhere no road reaches fails here, with the trip untouched.
- Adding nothing writes nothing, so a no-op cannot bump `edited_at` and mark discovery stale
  for no reason.

Undo is the rider deleting the waypoints it named. That is thin, and deliberately not
solved here with a private undo stack: the trip document is the undo model this app already
has, and a second one that only chat knows about would be worse than none.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from motorooter.planning.discovery.selection import above_the_floor, worth_routing_through
from motorooter.planning.insertion import insert_in_route_order
from motorooter.routing.errors import RouteIncomplete
from motorooter.routing.models import LegIntent, RouteLeg
from motorooter.trips.models import Poi, Trip, Waypoint
from motorooter.trips.service import edit_trip, legs_for, longest_routed_leg
from motorooter.trips.store import TripStore


class LegRouter(Protocol):
    """The routing service, narrowed to "can these points be joined".

    A protocol rather than the concrete router so a caller can prove it routed before it
    saved without standing up a provider registry — and so nothing here reaches for a
    provider by name, which is the thing the routing architecture exists to prevent.
    """

    async def route_waypoints(
        self,
        waypoints: tuple[Waypoint, ...],
        *,
        intent: LegIntent,
        provider_override: str | None = None,
    ) -> tuple[RouteLeg, ...]: ...


@dataclass(frozen=True)
class RoutedThrough:
    """What the edit did, in terms a rider can be shown."""

    trip: Trip
    """The trip as saved. Unchanged, and not rewritten, when nothing was added."""

    added: tuple[Poi, ...]
    """The places now on the route, **in route order** rather than by score.

    Ordered this way because it is read aloud: "through Lion Rock Lookout, then Stella's"
    describes a ride, and a leaderboard does not.
    """

    left_out: tuple[Poi, ...]
    """Places good enough to add that did not fit the count or the detour budget.

    Kept so the answer can be "and three more if you want them" rather than silence. A bound
    the rider cannot see reads as the search having found nothing.
    """


async def route_through_best(
    *,
    store: TripStore,
    slug: str,
    router: LegRouter,
    limit: int | None = None,
) -> RoutedThrough:
    """Add the best of the trip's discovered places to its route.

    Args:
        store: where the trip lives. The write is compare-and-swap, so a rider dragging the
            route while this runs does not lose their edit.
        slug: which trip.
        router: used to confirm the proposed route before it is saved, never to save it.
        limit: how many places at most. `None` takes the leg's own pace — see
            `selection.default_limit`.

    Raises:
        RouteIncomplete: no leg has geometry yet, so there is no corridor to measure
            against and nothing to insert into.
        RoutingError: the proposed route cannot be joined. Nothing is written.
    """
    trip = await store.get(slug)
    leg = longest_routed_leg(trip)
    if leg is None:
        raise RouteIncomplete(trip.unrouted_leg_indices or (0,))

    chosen = worth_routing_through(trip.pois, leg=leg, limit=limit)
    if not chosen:
        return RoutedThrough(trip=trip, added=(), left_out=_left_out(trip.pois, ()))

    waypoints = insert_in_route_order(
        trip.waypoints,
        [Waypoint(coordinate=place.coordinate, name=place.name) for place in chosen],
        geometry=leg.geometry,
    )
    await router.route_waypoints(waypoints, intent=_intent_of(trip))

    saved = await edit_trip(
        store,
        slug,
        waypoints=waypoints,
        legs=legs_for(trip, waypoints),
        pois=_marked_on_route(trip.pois, chosen),
    )
    return RoutedThrough(
        trip=saved,
        added=_in_route_order(chosen, waypoints),
        left_out=_left_out(trip.pois, chosen),
    )


def _intent_of(trip: Trip) -> LegIntent:
    return trip.legs[0].intent if trip.legs else LegIntent.TWISTY_PAVED


def _marked_on_route(pois: Sequence[Poi], chosen: Sequence[Poi]) -> tuple[Poi, ...]:
    """Every POI, with the chosen ones flagged as being on the route.

    The flag is what stops a second run adding the same place again, and it is what the map
    reads to draw a pinned place differently from a suggested one.
    """
    ids = {place.id for place in chosen}
    return tuple(
        place.model_copy(update={"on_route": True}) if place.id in ids else place for place in pois
    )


def _in_route_order(chosen: Sequence[Poi], waypoints: Sequence[Waypoint]) -> tuple[Poi, ...]:
    """`chosen`, sorted by where each ended up in the waypoint list."""
    order = {point.name: index for index, point in enumerate(waypoints)}
    return tuple(sorted(chosen, key=lambda place: order.get(place.name, len(order))))


def _left_out(pois: Sequence[Poi], chosen: Sequence[Poi]) -> tuple[Poi, ...]:
    """Places good enough to add that the count or the detour budget had no room for.

    Asks `above_the_floor` rather than reimplementing the floor, so "good enough" keeps one
    definition. This is what lets the answer be "and three more if you want them" instead of
    a silence indistinguishable from having found nothing.
    """
    taken = {place.id for place in chosen}
    return tuple(place for place in above_the_floor(pois) if place.id not in taken)
