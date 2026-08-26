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

from typing import Any, ClassVar

from pydantic import Field

from motorooter.llm.errors import ToolCallFailed
from motorooter.llm.tools import (
    ProgressReport,
    Tool,
    ToolArguments,
    ToolOutcome,
    ToolRegistry,
)
from motorooter.planning.discovery.errors import DiscoveryError
from motorooter.planning.discovery.lookup import FoundPlace, PlaceLookup
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.planning.route_through import LegRouter, route_through_best
from motorooter.planning.stitching import search_corridor
from motorooter.routing.errors import RouteIncomplete, RoutingError
from motorooter.routing.models import LegIntent
from motorooter.trips.models import Poi, PoiCategory, Trip, TripLeg, Waypoint
from motorooter.trips.service import changed_legs, edit_trip, legs_for
from motorooter.trips.store import TripStore

MINIMUM_WAYPOINTS = 2
"""Below this there is no route to compute, so removing the second-to-last is refused."""


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
        lookup: PlaceLookup | None = None,
    ) -> None:
        self._store = store
        self._slug = slug
        self._router = router
        self._discovery = discovery
        self._lookup = lookup

    async def _trip(self) -> Trip:
        return await self._store.get(self._slug)

    async def _find(self, text: str, trip: Trip) -> tuple[FoundPlace, ...]:
        """Places matching a name, biased toward where the trip already is.

        The bias is what makes "Leavenworth" mean the Washington one on a trip that is
        already in Washington. On an empty trip there is nothing to bias from, and inventing
        a centre would silently prefer one real place over another.
        """
        if self._lookup is None:
            msg = (
                "place lookup is not available: this deployment has no Places credentials "
                "configured, so waypoints can only be added from the map."
            )
            raise ToolCallFailed(msg)
        near = trip.waypoints[-1].coordinate if trip.waypoints else None
        try:
            return await self._lookup.search(text, near=near)
        except DiscoveryError as exc:
            msg = f"could not look up {text!r}: {exc}"
            raise ToolCallFailed(msg) from exc

    async def _routed_legs(
        self, trip: Trip, waypoints: tuple[Waypoint, ...]
    ) -> tuple[TripLeg, ...]:
        """The legs to save: every stretch this edit created, routed, and the rest untouched.

        Routing failure is the second check on geography: a verified place with no road near
        it fails here rather than becoming a pin nobody can ride to.

        **Only the stretches that changed.** This once routed the whole trip as one span,
        which cost a request the size of the route per waypoint added and, worse, refused
        edits for failures elsewhere — a live run was told its new stop could not be joined
        when the problem was a different stretch entirely.

        **And the geometry is kept.** The request has already been made; discarding the reply
        left the trip reporting zero distance until a browser routed it again. Legs the edit
        did not touch keep the geometry they had, which `legs_for` decides.

        A single point is not routed because there is nothing to route — but it is no longer
        unchecked, which is the part that was wrong. The old comment claimed the exemption
        closed itself, "the second waypoint routes against the first, so a bad start
        coordinate is caught then". It does not: a first waypoint 50 km off routes perfectly
        well *from* 50 km off, because the second validates the pair and not the guess. What
        makes a lone waypoint safe now is that its coordinate came from Places rather than
        from a model.
        """
        fresh = {
            (leg.start_waypoint_index, leg.end_waypoint_index)
            for leg in changed_legs(trip, waypoints)
        }
        legs: list[TripLeg] = []
        for leg in legs_for(trip, waypoints):
            if (leg.start_waypoint_index, leg.end_waypoint_index) not in fresh:
                legs.append(leg)
                continue
            span = (waypoints[leg.start_waypoint_index], waypoints[leg.end_waypoint_index])
            try:
                routed = await self._router.route_waypoints(span, intent=leg.intent)
            except RoutingError as exc:
                between = " and ".join(point.name or "an unnamed point" for point in span)
                msg = f"the stretch between {between} could not be routed: {exc}"
                raise ToolCallFailed(msg) from exc
            legs.append(
                leg.model_copy(
                    update={"routed": routed[0] if routed else None, "last_routing_error": None}
                )
            )
        return tuple(legs)


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

    async def run(self, arguments: Any, on_progress: ProgressReport | None = None) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
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
    name: str = Field(
        min_length=1,
        description=(
            "The place to add, by name: a town, a pass, a campground. It is looked up and "
            "verified before anything is added, so a name is all you can give — there is no "
            "coordinate argument."
        ),
    )
    place_id: str | None = Field(
        default=None,
        description=(
            "Only when a previous call returned several matches: the place_id of the one "
            "you meant. Must be one of the ids that call offered."
        ),
    )


class AddWaypoint(_TripTool):
    name: ClassVar[str] = "add_waypoint"
    description: ClassVar[str] = (
        "Add a place the route must pass through, by name, appended to the end of the trip. "
        "The name is looked up against Google Places first, so only somewhere real can be "
        "added — you cannot give a coordinate. If the name matches several places you will "
        "be shown them and can call again with the place_id you meant. Returns the full "
        "numbered waypoint list.\n\n"
        "Plotting a described route means calling this once per place, in order. Issue "
        "those calls together rather than waiting for each to return — the list you get "
        "back is the same either way, and a route is a dozen places."
    )
    arguments: ClassVar[type] = AddWaypointArguments

    async def run(self, arguments: Any, on_progress: ProgressReport | None = None) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        trip = await self._trip()
        found = await self._find(arguments.name, trip)
        chosen = _choose(found, arguments.name, arguments.place_id)
        point = Waypoint(coordinate=chosen.coordinate, name=chosen.name)

        proposed = (*trip.waypoints, point)
        legs = await self._routed_legs(trip, proposed)
        saved = await edit_trip(self._store, self._slug, waypoints=proposed, legs=legs)
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

    async def run(self, arguments: Any, on_progress: ProgressReport | None = None) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
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
            self._store, self._slug, waypoints=remaining, legs=legs_for(trip, remaining)
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

    async def run(self, arguments: Any, on_progress: ProgressReport | None = None) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
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


class SetRidingModeArguments(ToolArguments):
    mode: str = Field(
        description=(
            "Riding mode for the whole trip. 'highway_connector' is Fast, 'twisty_paved' is "
            "Twisties, 'unpaved' is Offroad. Only 'unpaved' reports what the road is made of."
        )
    )


class SetRidingMode(_TripTool):
    name: ClassVar[str] = "set_riding_mode"
    description: ClassVar[str] = (
        "Say what kind of trip this is: it becomes the mode for every leg, now and for any "
        "leg added later. Call this once when the rider states a preference — 'as much dirt "
        "as possible', 'keep it fast' — rather than setting each leg in turn. Use "
        "set_leg_intent afterwards only to make one section different from the rest."
    )
    arguments: ClassVar[type] = SetRidingModeArguments

    async def run(self, arguments: Any, on_progress: ProgressReport | None = None) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        """Record the mode on the trip and bring every existing leg into line.

        Both halves matter. Recording it is what survives the trip being rebuilt — the mode
        used to live only in legs that happened to exist, so stripping a trip back to one
        waypoint discarded it silently. Applying it is what the rider actually asked for, and
        doing it here rather than one `set_leg_intent` call per leg is the difference between
        one decision and six chances to get rate-limited into a paved leg.
        """
        trip = await self._trip()
        try:
            intent = LegIntent(arguments.mode)
        except ValueError as exc:
            available = ", ".join(sorted(item.value for item in LegIntent))
            msg = f"no riding mode named {arguments.mode!r}. Available modes: {available}"
            raise ToolCallFailed(msg) from exc

        legs: list[TripLeg] = []
        for leg in trip.legs:
            span = trip.waypoints[leg.start_waypoint_index : leg.end_waypoint_index + 1]
            try:
                routed = await self._router.route_waypoints(tuple(span), intent=intent)
            except RoutingError as exc:
                # Nothing is written. A leg carrying a dirt intent and paved geometry is
                # worse than either, and a half-applied mode is worse still.
                msg = f"leg {len(legs)} cannot be routed as {intent.value}: {exc}"
                raise ToolCallFailed(msg) from exc
            legs.append(
                leg.model_copy(update={"intent": intent, "routed": routed[0] if routed else None})
            )

        await edit_trip(self._store, self._slug, legs=tuple(legs), default_intent=intent)
        changed = f" {len(legs)} legs are now {intent.value}." if legs else ""
        return ToolOutcome(
            content=f"This is a {intent.value} trip.{changed} Legs added later will match.",
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

    async def run(self, arguments: Any, on_progress: ProgressReport | None = None) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
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
        legs = await self._routed_legs(trip, proposed)
        saved = await edit_trip(self._store, self._slug, waypoints=proposed, legs=legs)
        return ToolOutcome(
            content=f"Routing through {match.name}.\n{_numbered(saved)}",
            found=1,
            payload={"trip_changed": True},
        )


class RouteThroughBestArguments(ToolArguments):
    limit: int | None = Field(
        default=None,
        ge=0,
        le=20,
        description=(
            "How many places at most. Leave it out unless the rider asked for more or "
            "fewer: the default is paced to the length of the ride, roughly one stop every "
            "two hours."
        ),
    )


class RouteThroughBest(_TripTool):
    name: ClassVar[str] = "route_through_best"
    description: ClassVar[str] = (
        "Reroute the trip through the best of the places find_places already saved, chosen "
        "by their scores and inserted in the order the rider will meet them. Adds via-points "
        "only — the start and the destination stay as they are. Cheap and instant; it "
        "searches for nothing."
    )
    arguments: ClassVar[type] = RouteThroughBestArguments

    async def run(self, arguments: Any, on_progress: ProgressReport | None = None) -> ToolOutcome:  # noqa: ANN401 -- narrowed by base
        """Reroute through the best places, and say plainly which and why.

        The tool is thin — the selection, the bounds and the write all live in
        `route_through_best`, which the button calls too. What is here is the telling: this
        is an autonomous edit to a route somebody built, so a reply that says "done" is not
        good enough. Each addition is named with the judge's own sentence, and the numbered
        waypoint list follows so the model's indices survive the edit.
        """
        try:
            result = await route_through_best(
                store=self._store, slug=self._slug, router=self._router, limit=arguments.limit
            )
        except RouteIncomplete as exc:
            msg = f"the trip has no routed geometry yet, so there is no route to add to: {exc}"
            raise ToolCallFailed(msg) from exc
        except RoutingError as exc:
            msg = f"the trip could not be rerouted through those places: {exc}"
            raise ToolCallFailed(msg) from exc

        if not result.added:
            return ToolOutcome(content=f"Nothing worth rerouting through.{_spare(result.left_out)}")

        lines = [f"Routing through {len(result.added)} places:"]
        lines += [
            f"  {place.name} — {place.note or 'no reason recorded'}" for place in result.added
        ]
        return ToolOutcome(
            content="\n".join(lines) + _spare(result.left_out) + "\n" + _numbered(result.trip),
            found=len(result.added),
            payload={"trip_changed": True},
        )


def _spare(left_out: tuple[Poi, ...]) -> str:
    """What was good enough but did not fit, so a bound the rider cannot see is not silence."""
    if not left_out:
        return ""
    return f" {len(left_out)} more scored well; ask for a higher limit to include them."


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

    reports_progress: ClassVar[bool] = True
    """The only tool slow enough to need it: tens of seconds against milliseconds."""

    async def run(
        self,
        arguments: Any,  # noqa: ANN401 -- narrowed by the subclass's arguments model
        on_progress: ProgressReport | None = None,
    ) -> ToolOutcome:
        """Search along the route, reporting as it goes.

        The slowest thing the assistant can do — tens of seconds — and the only tool that
        takes a progress callback. The pipeline has always emitted these events; this stage
        used to consume the stream and keep only the last, so the same work that fills a bar
        from the Replan button showed nothing at all in the chat rail.
        """
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
        corridor = search_corridor(trip)
        if corridor is None:
            msg = "the trip has no routed geometry yet, so there is no corridor to search"
            raise ToolCallFailed(msg)

        found: tuple[Poi, ...] = ()
        summary = ""
        async for progress in self._discovery.run(corridor, categories):
            if progress.pois:
                found = progress.pois
            summary = progress.message
            if on_progress is not None:
                on_progress(progress.message, progress.progress)

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


def _choose(found: tuple[FoundPlace, ...], text: str, place_id: str | None) -> FoundPlace:
    """The one place meant, or a refusal listing the candidates.

    Ambiguity is refused rather than resolved. Pinning the best of several real matches is
    the same failure the coordinate argument used to allow, one layer up: a plausible
    location, nothing visibly wrong, and a route to the wrong Leavenworth.

    Choosing among verified candidates is judgement the model may exercise on a second call.
    Producing a location is not.
    """
    if not found:
        msg = (
            f"nothing called {text!r} could be found. Check the spelling, or try a nearby "
            "town or a named road instead."
        )
        raise ToolCallFailed(msg)

    if place_id is not None:
        match = next((item for item in found if item.place_id == place_id), None)
        if match is None:
            offered = ", ".join(item.place_id for item in found)
            msg = f"place_id {place_id!r} was not among the matches. They were: {offered}"
            raise ToolCallFailed(msg)
        return match

    if len(found) == 1:
        return found[0]

    lines = [
        f"  {item.place_id}  {item.name}" + (f" — {item.address}" if item.address else "")
        for item in found
    ]
    msg = (
        f"{text!r} matches {len(found)} places and nothing was added. Call again with the "
        "place_id of the one you mean:\n" + "\n".join(lines)
    )
    raise ToolCallFailed(msg)


class TripTools:
    """The tools, bound to one trip and published as a registry."""

    def __init__(
        self,
        *,
        store: TripStore,
        slug: str,
        router: LegRouter,
        discovery: DiscoveryPipeline | None = None,
        lookup: PlaceLookup | None = None,
    ) -> None:
        self.store = store
        self.slug = slug

        def build(tool: type[_TripTool]) -> _TripTool:
            return tool(store=store, slug=slug, router=router, discovery=discovery, lookup=lookup)

        self.registry = ToolRegistry(
            [
                build(DescribeTrip),
                build(FindPlaces),
                build(AddWaypoint),
                build(RemoveWaypoint),
                build(SetRidingMode),
                build(SetLegIntent),
                build(AddPoiToRoute),
                build(RouteThroughBest),
            ]
        )
