"""The tools the assistant can call against one trip.

**Every tool is a thin wrapper over the same service function the REST endpoint calls.** That
is the rule the whole design rests on and it is the only reason these are worth having: the
mouse path and the chat path must not diverge, and two implementations that agree today
diverge by next week while both keep answering plausibly. Where the logic did not exist as a
function it was extracted first — `trips.service.edit_trip` is the router's implementation,
not a copy of it.

Three properties every tool here holds to.

**Model geography is a claim, not a fact.** A coordinate arrives from a language model and is
routed before it is saved; a place is named by `place_id` already resolved onto the trip,
never by a coordinate the model supplies. `Poi` refuses unverified suggestions at its own
layer and this layer must not reintroduce them one level up.

**Writes go through compare-and-swap.** The rider may be dragging the route while the
assistant edits it. `edit_trip` carries the version it read, so a chat edit cannot silently
roll back a drag.

**A tool that moves waypoints returns the numbered list.** Compare-and-swap catches the write
race; it does not catch the model holding index 3 for a waypoint that is now index 4 because
the rider added one mid-conversation. That write succeeds and removes the wrong thing. The
list re-anchors the model after every mutation, which a prose summary does not.
"""

from typing import Any, ClassVar, Protocol

from pydantic import Field, ValidationError

from motorooter.llm.errors import ToolCallFailed
from motorooter.llm.tools import Tool, ToolArguments, ToolOutcome, ToolRegistry
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.routing.errors import RoutingError
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import Poi, PoiCategory, Trip, TripLeg, Waypoint
from motorooter.trips.service import edit_trip
from motorooter.trips.store import TripStore

MINIMUM_WAYPOINTS = 2
"""Below this there is no route to compute, so removing the second-to-last is refused."""


class LegRouter(Protocol):
    """The routing service, narrowed to what these tools need.

    A protocol rather than the concrete router so a test can prove a tool routed before it
    saved without standing up a provider registry — and so no tool reaches for a provider by
    name, which is the thing the routing architecture exists to prevent.
    """

    async def route_waypoints(
        self,
        waypoints: tuple[Waypoint, ...],
        *,
        intent: LegIntent,
        provider_override: str | None = None,
    ) -> tuple[RouteLeg, ...]: ...


def _numbered(trip: Trip) -> str:
    """The waypoint list, as the model must see it after any edit that moves indices."""
    if not trip.waypoints:
        return "The trip has no waypoints."
    lines = [
        f"  [{index}] {point.name or 'unnamed'} "
        f"({point.coordinate.lat:.4f}, {point.coordinate.lon:.4f})"
        for index, point in enumerate(trip.waypoints)
    ]
    return "Waypoints are now:\n" + "\n".join(lines)


class _TripTool(Tool):
    """Shared plumbing: which trip, which store, which router."""

    def __init__(
        self,
        *,
        store: TripStore,
        slug: str,
        router: LegRouter,
        discovery: DiscoveryPipeline | None = None,
    ) -> None:
        self._store = store
        self._slug = slug
        self._router = router
        self._discovery = discovery

    async def _trip(self) -> Trip:
        return await self._store.get(self._slug)

    async def _route_all(self, trip: Trip, waypoints: tuple[Waypoint, ...]) -> None:
        """Confirm the waypoints can actually be joined, before anything is written.

        Routing failure is the check on model-invented geography: a point in the sea, or one
        350 m from any road, fails here rather than becoming a pin nobody can ride to.

        A single point is exempt because there is nothing to route — a trip has to start
        somewhere, and refusing the first waypoint would make it impossible to begin one by
        chat at all. The exemption closes itself: the second waypoint routes against the
        first, so a bad start coordinate is caught then rather than never.
        """
        if len(waypoints) < MINIMUM_WAYPOINTS:
            return
        intent = trip.legs[0].intent if trip.legs else LegIntent.TWISTY_PAVED
        try:
            await self._router.route_waypoints(waypoints, intent=intent)
        except RoutingError as exc:
            msg = f"those points could not be joined into a route: {exc}"
            raise ToolCallFailed(msg) from exc


class DescribeTripArguments(ToolArguments):
    pass


class DescribeTrip(_TripTool):
    name: ClassVar[str] = "describe_trip"
    description: ClassVar[str] = (
        "Report the current trip: its waypoints in order with their indices, each leg's "
        "riding mode, total distance, and how much of it is unpaved, paved or unsurveyed. "
        "Call this before answering any question about the trip's length, shape or surface, "
        "and after any edit you did not make yourself."
    )
    arguments: ClassVar[type] = DescribeTripArguments

    async def run(self, arguments: Any) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        trip = await self._trip()
        lines = [f"Trip {trip.name!r} ({trip.slug})", _numbered(trip)]

        if trip.legs:
            lines.append("Legs:")
            for index, leg in enumerate(trip.legs):
                routed = leg.routed
                distance = f", {routed.distance_m / 1000:.1f} km" if routed else ", not yet routed"
                lines.append(
                    f"  [{index}] waypoints {leg.start_waypoint_index}-"
                    f"{leg.end_waypoint_index}, mode {leg.intent.value}{distance}"
                )

        lines.append(_surface_line(trip))
        lines.append(f"{len(trip.pois)} places saved against this trip.")
        return ToolOutcome(content="\n".join(lines), found=len(trip.pois))


def _surface_line(trip: Trip) -> str:
    """Surface as three shares, never two.

    `unpaved_fraction` counts only spans explicitly tagged unpaved, and an OSM audit found a
    quarter of one BDR section carries no surface tag at all. Folding the unknown share into
    "paved" would let the model tell a rider a route is 60% paved when a quarter of it has
    never been surveyed — a materially different proposition, and the reason the UI shows
    three numbers.
    """
    routed = [leg.routed for leg in trip.legs if leg.routed is not None]
    if not routed:
        return "Surface: not yet routed, so nothing is known about it."

    # The domain computes all three, and computes `unknown` as the remainder rather than
    # summing UNKNOWN spans — geometry no span covers is exactly as unsurveyed as geometry
    # tagged unsurveyed. Recomputing it here got it wrong once already.
    total = sum(leg.geometry_length_m for leg in routed)
    if total <= 0:
        return "Surface: unknown."
    unpaved = sum(leg.unpaved_distance_m for leg in routed)
    known_paved = sum(leg.paved_distance_m for leg in routed)
    unknown = sum(leg.unknown_distance_m for leg in routed)
    return (
        f"Total {total / 1000:.1f} km: {unpaved / total:.0%} unpaved, "
        f"{known_paved / total:.0%} paved, {unknown / total:.0%} unsurveyed. "
        "Unsurveyed means the map has no surface tag there, not that it is paved."
    )


class AddWaypointArguments(ToolArguments):
    lat: float = Field(ge=-90.0, le=90.0, description="Latitude in decimal degrees.")
    lon: float = Field(ge=-180.0, le=180.0, description="Longitude in decimal degrees.")
    name: str | None = Field(default=None, description="Optional label for the waypoint.")


class AddWaypoint(_TripTool):
    name: ClassVar[str] = "add_waypoint"
    description: ClassVar[str] = (
        "Add a point the route must pass through, appended to the end of the trip. The "
        "coordinate is routed before it is saved, so a point with no road near it will be "
        "refused rather than added. Returns the full numbered waypoint list."
    )
    arguments: ClassVar[type] = AddWaypointArguments

    async def run(self, arguments: Any) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        trip = await self._trip()
        try:
            point = Waypoint(
                coordinate=Coordinate(lat=arguments.lat, lon=arguments.lon),
                name=arguments.name,
            )
        except ValidationError as exc:
            msg = f"that is not a valid coordinate: {exc.error_count()} problem(s)"
            raise ToolCallFailed(msg) from exc

        proposed = (*trip.waypoints, point)
        await self._route_all(trip, proposed)
        saved = await edit_trip(
            self._store, self._slug, waypoints=proposed, legs=_legs_for(trip, proposed)
        )
        return ToolOutcome(
            content=f"Added waypoint {len(saved.waypoints) - 1}.\n{_numbered(saved)}",
            found=1,
            payload={"trip_changed": True},
        )


class RemoveWaypointArguments(ToolArguments):
    index: int = Field(ge=0, description="Which waypoint to remove, from describe_trip.")


class RemoveWaypoint(_TripTool):
    name: ClassVar[str] = "remove_waypoint"
    description: ClassVar[str] = (
        "Remove one waypoint by its index. Indices come from describe_trip or from the list "
        "returned by the last edit — they shift whenever a waypoint is added or removed, "
        "including by the rider, so never reuse an index from earlier in the conversation. "
        "Returns the full numbered waypoint list."
    )
    arguments: ClassVar[type] = RemoveWaypointArguments

    async def run(self, arguments: Any) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        trip = await self._trip()
        if arguments.index >= len(trip.waypoints):
            msg = (
                f"there is no waypoint {arguments.index}; the trip has "
                f"{len(trip.waypoints)}. Call describe_trip for the current list."
            )
            raise ToolCallFailed(msg)
        if len(trip.waypoints) <= MINIMUM_WAYPOINTS:
            msg = (
                f"a trip needs at least {MINIMUM_WAYPOINTS} waypoints to be a route, and "
                f"this one has {len(trip.waypoints)}"
            )
            raise ToolCallFailed(msg)

        remaining = tuple(
            point for index, point in enumerate(trip.waypoints) if index != arguments.index
        )
        saved = await edit_trip(
            self._store, self._slug, waypoints=remaining, legs=_legs_for(trip, remaining)
        )
        return ToolOutcome(
            content=f"Removed waypoint {arguments.index}.\n{_numbered(saved)}",
            payload={"trip_changed": True},
        )


class SetLegIntentArguments(ToolArguments):
    leg_index: int = Field(ge=0, description="Which leg, from describe_trip.")
    intent: str = Field(
        description=(
            "Riding mode. 'highway_connector' is Fast, 'twisty_paved' is Twisties, "
            "'unpaved' is Offroad. Only 'unpaved' reports what the road is made of."
        )
    )


class SetLegIntent(_TripTool):
    name: ClassVar[str] = "set_leg_intent"
    description: ClassVar[str] = (
        "Change how one leg is routed and re-route it. Mode is per leg, not per trip, so a "
        "rider can have dirt in the middle of a paved day."
    )
    arguments: ClassVar[type] = SetLegIntentArguments

    async def run(self, arguments: Any) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        trip = await self._trip()
        try:
            intent = LegIntent(arguments.intent)
        except ValueError as exc:
            available = ", ".join(sorted(item.value for item in LegIntent))
            msg = f"no riding mode named {arguments.intent!r}. Available modes: {available}"
            raise ToolCallFailed(msg) from exc

        if arguments.leg_index >= len(trip.legs):
            msg = (
                f"there is no leg {arguments.leg_index}; the trip has {len(trip.legs)}. "
                "Call describe_trip for the current list."
            )
            raise ToolCallFailed(msg)

        leg = trip.legs[arguments.leg_index]
        span = trip.waypoints[leg.start_waypoint_index : leg.end_waypoint_index + 1]
        try:
            routed = await self._router.route_waypoints(tuple(span), intent=intent)
        except RoutingError as exc:
            msg = f"that leg cannot be routed as {intent.value}: {exc}"
            raise ToolCallFailed(msg) from exc

        legs = list(trip.legs)
        legs[arguments.leg_index] = leg.model_copy(
            update={"intent": intent, "routed": routed[0] if routed else None}
        )
        await edit_trip(self._store, self._slug, legs=tuple(legs))
        return ToolOutcome(
            content=f"Leg {arguments.leg_index} is now {intent.value}.",
            payload={"trip_changed": True},
        )


class AddPoiToRouteArguments(ToolArguments):
    place_id: str = Field(
        min_length=1,
        description=(
            "The place_id of a place already saved against this trip, as listed by "
            "find_places or describe_trip. Not a coordinate and not a name."
        ),
    )


class AddPoiToRoute(_TripTool):
    name: ClassVar[str] = "add_poi_to_route"
    description: ClassVar[str] = (
        "Route the trip through a place that discovery already found, by its place_id. The "
        "place must already be saved against the trip; this tool cannot add somewhere that "
        "has not been verified against Google Places."
    )
    arguments: ClassVar[type] = AddPoiToRouteArguments

    async def run(self, arguments: Any) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        trip = await self._trip()
        match = next((poi for poi in trip.pois if poi.place_id == arguments.place_id), None)
        if match is None:
            # Deliberately not "find it for me": accepting an unknown id here would let a
            # model put a place on the map that nothing had verified, which is the exact
            # failure the Poi model refuses one layer down.
            msg = (
                f"no place with place_id {arguments.place_id!r} is saved against this trip. "
                "Run find_places first, then use an id from its results."
            )
            raise ToolCallFailed(msg)

        point = Waypoint(coordinate=match.coordinate, name=match.name)
        proposed = (*trip.waypoints, point)
        await self._route_all(trip, proposed)
        saved = await edit_trip(
            self._store, self._slug, waypoints=proposed, legs=_legs_for(trip, proposed)
        )
        return ToolOutcome(
            content=f"Routing through {match.name}.\n{_numbered(saved)}",
            found=1,
            payload={"trip_changed": True},
        )


class FindPlacesArguments(ToolArguments):
    categories: list[str] = Field(
        min_length=1,
        description=(
            "What to look for along the route: wild_camp, campground, hotel, food, fuel, "
            "viewpoint. Ask for only what was requested — each category costs a round of "
            "searches at every point along the corridor."
        ),
    )


class FindPlaces(_TripTool):
    name: ClassVar[str] = "find_places"
    description: ClassVar[str] = (
        "Search along the trip's route for places worth stopping at, verify each one against "
        "Google Places, and save what survives to the trip. This is the same search the "
        "Replan button runs. Slow — tens of seconds — so call it once with every category "
        "the rider asked for rather than once per category."
    )
    arguments: ClassVar[type] = FindPlacesArguments

    async def run(self, arguments: Any) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        if self._discovery is None:
            msg = (
                "place search is not available: this deployment has no search credentials "
                "configured. Everything else about the trip still works."
            )
            raise ToolCallFailed(msg)

        try:
            categories = [PoiCategory(value) for value in arguments.categories]
        except ValueError as exc:
            available = ", ".join(sorted(item.value for item in PoiCategory))
            msg = f"unknown category. Available categories: {available}"
            raise ToolCallFailed(msg) from exc

        trip = await self._trip()
        routed = [leg.routed for leg in trip.legs if leg.routed is not None]
        if not routed:
            msg = "the trip has no routed geometry yet, so there is no corridor to search"
            raise ToolCallFailed(msg)

        found: tuple[Poi, ...] = ()
        summary = ""
        async for progress in self._discovery.run(routed[0], categories):
            if progress.pois:
                found = progress.pois
            summary = progress.message

        if not found:
            return ToolOutcome(content=f"Found nothing worth pinning. {summary}")

        saved = await edit_trip(self._store, self._slug, pois=(*trip.pois, *found))
        lines = [f"Found {len(found)} places. {summary}", "Saved to the trip:"]
        lines += [f"  {poi.name} ({poi.category.value}) place_id={poi.place_id}" for poi in found]
        return ToolOutcome(
            content="\n".join(lines),
            found=len(found),
            payload={"trip_changed": True, "pois": len(saved.pois)},
        )


def _legs_for(trip: Trip, waypoints: tuple[Waypoint, ...]) -> tuple[TripLeg, ...]:
    """Legs spanning consecutive waypoint pairs, inheriting the trip's existing intent.

    Rebuilt rather than patched because indices shift: a leg recorded as 2-3 means a
    different stretch of road once a waypoint is removed ahead of it. Geometry is dropped —
    the leg is stale by definition — and the next route request fills it back in.
    """
    if len(waypoints) < MINIMUM_WAYPOINTS:
        return ()
    default = trip.legs[0].intent if trip.legs else LegIntent.TWISTY_PAVED
    existing = {(leg.start_waypoint_index, leg.end_waypoint_index): leg for leg in trip.legs}
    return tuple(
        TripLeg(
            intent=existing[(index, index + 1)].intent
            if (index, index + 1) in existing
            else default,
            start_waypoint_index=index,
            end_waypoint_index=index + 1,
        )
        for index in range(len(waypoints) - 1)
    )


class TripTools:
    """The six tools, bound to one trip and published as a registry."""

    def __init__(
        self,
        *,
        store: TripStore,
        slug: str,
        router: LegRouter,
        discovery: DiscoveryPipeline | None = None,
    ) -> None:
        self.store = store
        self.slug = slug

        def build(tool: type[_TripTool]) -> _TripTool:
            return tool(store=store, slug=slug, router=router, discovery=discovery)

        self.registry = ToolRegistry(
            [
                build(DescribeTrip),
                build(FindPlaces),
                build(AddWaypoint),
                build(RemoveWaypoint),
                build(SetLegIntent),
                build(AddPoiToRoute),
            ]
        )
