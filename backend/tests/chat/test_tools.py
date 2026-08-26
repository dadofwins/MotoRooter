"""The six tools the assistant can call.

What is tested here is mostly not "does it work" — the service functions beneath have their
own tests — but the three properties that make a tool different from a function call:

**It shares the endpoint's implementation.** A tool that reimplements an endpoint agrees with
it today and diverges silently later, because both keep returning something plausible.

**It refuses LLM geography.** A coordinate from a model is a claim. Nothing reaches the trip
without a real routing or Places call having agreed with it first.

**It tells the model what the trip looks like afterwards.** Indices shift under every edit,
including ones the rider makes with the mouse mid-conversation, so a tool that mutates
waypoints and returns prose leaves the model holding a number that now means something else.
"""

import re

import pytest

from motorooter.chat.tools import (
    AddPoiToRoute,
    AddWaypoint,
    DescribeTrip,
    FindPlaces,
    RemoveWaypoint,
    SetLegIntent,
    SetRidingMode,
    TripTools,
)
from motorooter.llm.errors import ToolCallFailed
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.routing.errors import NoRouteFound, ProviderUnavailable
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import (
    DEFAULT_INTENT,
    Poi,
    PoiCategory,
    PoiSource,
    Trip,
    TripLeg,
    Waypoint,
    utc_now,
)
from motorooter.trips.store import InMemoryTripStore

SLUG = "cascade-loop"

THREE_POINTS = (
    Waypoint(coordinate=Coordinate(lat=47.0, lon=-121.0), name="Start"),
    Waypoint(coordinate=Coordinate(lat=47.1, lon=-121.1), name="Middle"),
    Waypoint(coordinate=Coordinate(lat=47.2, lon=-121.2), name="End"),
)

TWO_PAVED_LEGS = (
    TripLeg(intent=LegIntent.TWISTY_PAVED, start_waypoint_index=0, end_waypoint_index=1),
    TripLeg(intent=LegIntent.TWISTY_PAVED, start_waypoint_index=1, end_waypoint_index=2),
)


def coordinate(lat: float = 47.0, lon: float = -121.0) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


def trip(*, waypoints=None, legs=None, pois=(), routed=False, default_intent=None) -> Trip:
    now = utc_now()
    points = (
        waypoints
        if waypoints is not None
        else (
            Waypoint(coordinate=coordinate(47.0, -121.0), name="Start"),
            Waypoint(coordinate=coordinate(47.1, -121.1), name="End"),
        )
    )
    return Trip(
        slug=SLUG,
        name="Cascade Loop",
        created_at=now,
        edited_at=now,
        waypoints=points,
        legs=legs
        if legs is not None
        else (
            TripLeg(
                intent=LegIntent.TWISTY_PAVED,
                start_waypoint_index=0,
                end_waypoint_index=1,
                routed=routed_leg() if routed else None,
            ),
        ),
        pois=pois,
        default_intent=default_intent,
    )


def routed_leg() -> RouteLeg:
    return RouteLeg(
        geometry=(coordinate(47.0, -121.0), coordinate(47.05, -121.05)),
        distance_m=8000.0,
        duration_s=600.0,
        provider="fake",
        intent=LegIntent.TWISTY_PAVED,
    )


class StubRouter:
    """Stands in for the routing service. Records what it was asked to prove the tool asked."""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[Coordinate, ...]] = []

    async def route_waypoints(self, waypoints, *, intent, provider_override=None):
        self.calls.append(tuple(w.coordinate for w in waypoints))
        if self.error is not None:
            raise self.error
        return (routed_leg(),) * max(len(waypoints) - 1, 1)


async def store_with(document: Trip) -> InMemoryTripStore:
    store = InMemoryTripStore()
    await store.create(document)
    return store


async def tools(
    *, document: Trip | None = None, router=None, discovery=None, lookup=None
) -> TripTools:
    store = await store_with(document or trip())
    return TripTools(
        store=store,
        slug=SLUG,
        router=router or StubRouter(),
        discovery=discovery,
        lookup=lookup,
    )


async def call(kit: TripTools, name: str, raw: str):
    """Invoke a tool the way the agent does — resolve, parse, run — rather than by a path
    only tests use. A convenience method here would be a second call path to keep correct."""
    tool = kit.registry.get(name)
    return await tool.run(tool.parse(raw))


class TestDescribeTrip:
    """Read-only, and the reason it exists is the surface figures.

    Without it the model answers "how long is this?" from whatever the conversation happens
    to contain. The unpaved fraction in particular has a rule — unknown stays unknown — that
    it will otherwise round away.
    """

    async def test_it_reports_the_waypoints_in_order(self):
        kit = await tools()
        outcome = await call(kit, DescribeTrip.name, "{}")
        assert "Start" in outcome.content
        assert "End" in outcome.content

    async def test_it_reports_each_leg_intent(self):
        kit = await tools()
        outcome = await call(kit, DescribeTrip.name, "{}")
        assert "twisty_paved" in outcome.content

    async def test_it_does_not_change_the_trip(self):
        kit = await tools()
        outcome = await call(kit, DescribeTrip.name, "{}")
        assert outcome.payload.get("trip_changed") is not True


def a_place(name="Blewett Pass", place_id="ChIJ_bp", lat=47.34, lon=-120.58):
    from motorooter.planning.discovery.lookup import FoundPlace

    return FoundPlace(
        name=name, place_id=place_id, coordinate=Coordinate(lat=lat, lon=lon), kinds=("route",)
    )


class OneResult:
    """A lookup that finds exactly the place asked for."""

    def __init__(self, *found):
        self.found = found or (a_place(),)

    async def search(self, text, *, near=None, limit=5):
        return self.found


class TestAddWaypoint:
    async def test_it_routes_before_it_saves(self):
        """Places says the somewhere is real; routing says it is reachable. Both, in order."""
        router = StubRouter()
        kit = await tools(router=router, lookup=OneResult())
        await call(kit, AddWaypoint.name, '{"name": "Blewett Pass"}')
        assert router.calls, "saved without routing"

    async def test_an_unroutable_place_is_not_saved(self):
        """A real place with no road near it. Verified is not the same as reachable."""
        router = StubRouter(error=NoRouteFound("no road within 350 m"))
        kit = await tools(router=router, lookup=OneResult())
        with pytest.raises(ToolCallFailed):
            await call(kit, AddWaypoint.name, '{"name": "Blewett Pass"}')
        assert len((await kit.store.get(SLUG)).waypoints) == 2

    async def test_the_waypoint_is_added(self):
        kit = await tools(lookup=OneResult())
        await call(kit, AddWaypoint.name, '{"name": "Blewett Pass"}')
        assert len((await kit.store.get(SLUG)).waypoints) == 3

    async def test_it_is_named_as_places_names_it(self):
        """Not as the rider typed it, so "blewett" does not become a waypoint called
        "blewett" on the device."""
        kit = await tools(lookup=OneResult(a_place(name="Blewett Pass")))
        await call(kit, AddWaypoint.name, '{"name": "blewett"}')
        assert (await kit.store.get(SLUG)).waypoints[-1].name == "Blewett Pass"

    async def test_it_returns_the_numbered_list(self):
        """Indices shift under every edit, including the rider's. A prose summary leaves the
        model holding a number that now means a different waypoint."""
        kit = await tools(lookup=OneResult())
        outcome = await call(kit, AddWaypoint.name, '{"name": "Blewett Pass"}')
        assert "0" in outcome.content and "2" in outcome.content


class TestRemoveWaypoint:
    async def test_it_removes_by_index(self):
        kit = await tools(
            document=trip(
                waypoints=(
                    Waypoint(coordinate=coordinate(47.0, -121.0), name="Start"),
                    Waypoint(coordinate=coordinate(47.05, -121.05), name="Middle"),
                    Waypoint(coordinate=coordinate(47.1, -121.1), name="End"),
                )
            )
        )
        await call(kit, RemoveWaypoint.name, '{"index": 1}')
        remaining = (await kit.store.get(SLUG)).waypoints
        assert [w.name for w in remaining] == ["Start", "End"]

    async def test_an_index_that_does_not_exist_is_reported_to_the_model(self):
        kit = await tools()
        with pytest.raises(ToolCallFailed):
            await call(kit, RemoveWaypoint.name, '{"index": 9}')

    async def test_a_trip_needs_two_waypoints(self):
        """Removing down to one leaves something that cannot be routed at all."""
        kit = await tools()
        with pytest.raises(ToolCallFailed):
            await call(kit, RemoveWaypoint.name, '{"index": 0}')

    async def test_it_returns_the_numbered_list(self):
        kit = await tools(
            document=trip(
                waypoints=(
                    Waypoint(coordinate=coordinate(47.0, -121.0), name="Start"),
                    Waypoint(coordinate=coordinate(47.05, -121.05), name="Middle"),
                    Waypoint(coordinate=coordinate(47.1, -121.1), name="End"),
                )
            )
        )
        outcome = await call(kit, RemoveWaypoint.name, '{"index": 1}')
        assert "Start" in outcome.content and "End" in outcome.content
        assert "Middle" not in outcome.content


class TestTheTripRemembersItsMode:
    """Where the mode lives, and why it cannot only live on legs.

    A trip built from nothing has no legs to inherit from, so its first leg came from a
    hardcoded constant — `twisty_paved`, chosen in the tool layer, disagreeing with the
    frontend's `unpaved`, which was chosen on the grounds that dirt is the point of the
    product. So **every chat-built trip was born paved**, and stayed paved unless the model
    remembered to correct it afterwards, one `set_leg_intent` call and one routing request
    per leg. On a six-leg trip that is six chances to do five of them.

    `Trip.default_intent` is where the answer lives now, and `DEFAULT_INTENT` is the one
    value it falls back to.
    """

    async def test_a_trip_built_from_nothing_is_not_born_paved(self):
        kit = await tools(lookup=OneResult(), document=trip(waypoints=(), legs=()))
        await call(kit, AddWaypoint.name, '{"name": "Ellensburg"}')
        await call(kit, AddWaypoint.name, '{"name": "Cle Elum"}')
        assert [leg.intent for leg in (await kit.store.get(SLUG)).legs] == [DEFAULT_INTENT]

    async def test_a_new_leg_takes_the_mode_the_rider_stated(self):
        kit = await tools(
            lookup=OneResult(), document=trip(default_intent=LegIntent.HIGHWAY_CONNECTOR)
        )
        await call(kit, AddWaypoint.name, '{"name": "Cle Elum"}')
        assert (await kit.store.get(SLUG)).legs[-1].intent is LegIntent.HIGHWAY_CONNECTOR

    async def test_an_unstated_mode_falls_back_to_the_products_default(self):
        """One rule, not two. Inheriting from leg 0 is what the missing field stood in for,
        and it is the rule with the cliff: no legs, nothing to inherit, silent pavement."""
        kit = await tools(
            lookup=OneResult(),
            document=trip(
                legs=(
                    TripLeg(
                        intent=LegIntent.HIGHWAY_CONNECTOR,
                        start_waypoint_index=0,
                        end_waypoint_index=1,
                    ),
                )
            ),
        )
        await call(kit, AddWaypoint.name, '{"name": "Cle Elum"}')
        legs = (await kit.store.get(SLUG)).legs
        assert legs[0].intent is LegIntent.HIGHWAY_CONNECTOR  # a stated leg is left alone
        assert legs[-1].intent is DEFAULT_INTENT

    async def test_a_stated_mode_beats_the_default(self):
        kit = await tools(
            lookup=OneResult(),
            document=trip(
                default_intent=LegIntent.HIGHWAY_CONNECTOR,
                legs=(
                    TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1),
                ),
            ),
        )
        await call(kit, AddWaypoint.name, '{"name": "Cle Elum"}')
        assert (await kit.store.get(SLUG)).legs[-1].intent is LegIntent.HIGHWAY_CONNECTOR

    async def test_the_mode_survives_the_route_being_cleared_and_replotted(self):
        """The map can strip a trip to a single point; chat cannot, but the rider can."""
        kit = await tools(
            lookup=OneResult(), document=trip(default_intent=LegIntent.HIGHWAY_CONNECTOR)
        )
        emptied = (await kit.store.get(SLUG)).model_copy(
            update={"waypoints": THREE_POINTS[:1], "legs": ()}
        )
        await kit.store.put(emptied)
        await call(kit, AddWaypoint.name, '{"name": "Cle Elum"}')
        assert (await kit.store.get(SLUG)).legs[-1].intent is LegIntent.HIGHWAY_CONNECTOR


class TestSetRidingMode:
    """One call to say what kind of trip this is, instead of one call per leg.

    Per-leg was the only way to say it, and it costs a routing request each: on a six-leg
    trip that is six chances to be rate-limited into a paved leg, and it is forgotten the
    moment the trip is rebuilt.
    """

    async def test_it_records_the_mode_on_the_trip(self):
        kit = await tools()
        await call(kit, SetRidingMode.name, '{"mode": "unpaved"}')
        assert (await kit.store.get(SLUG)).default_intent is LegIntent.UNPAVED

    async def test_it_applies_the_mode_to_every_existing_leg(self):
        kit = await tools(document=trip(waypoints=THREE_POINTS, legs=TWO_PAVED_LEGS))
        await call(kit, SetRidingMode.name, '{"mode": "unpaved"}')
        legs = (await kit.store.get(SLUG)).legs
        assert [leg.intent for leg in legs] == [LegIntent.UNPAVED, LegIntent.UNPAVED]

    async def test_it_reroutes_each_leg_it_changed(self):
        """A leg carrying dirt intent and paved geometry is worse than either."""
        router = StubRouter()
        kit = await tools(document=trip(waypoints=THREE_POINTS, legs=TWO_PAVED_LEGS), router=router)
        await call(kit, SetRidingMode.name, '{"mode": "unpaved"}')
        assert len(router.calls) == 2

    async def test_an_unknown_mode_is_refused(self):
        kit = await tools()
        with pytest.raises(ToolCallFailed, match="riding mode"):
            await call(kit, SetRidingMode.name, '{"mode": "hover"}')

    async def test_a_trip_with_no_legs_still_records_the_mode(self):
        """Stating the mode before plotting anything is the order that avoids the bug."""
        kit = await tools(
            document=trip(waypoints=(Waypoint(coordinate=coordinate(), name="Start"),), legs=())
        )
        await call(kit, SetRidingMode.name, '{"mode": "unpaved"}')
        assert (await kit.store.get(SLUG)).default_intent is LegIntent.UNPAVED

    async def test_nothing_is_saved_if_a_leg_will_not_route_that_way(self):
        router = StubRouter(error=ProviderUnavailable("down"))
        kit = await tools(document=trip(), router=router)
        with pytest.raises(ToolCallFailed):
            await call(kit, SetRidingMode.name, '{"mode": "unpaved"}')
        assert (await kit.store.get(SLUG)).default_intent is None


class TestSetLegIntent:
    async def test_it_changes_the_intent(self):
        kit = await tools()
        await call(kit, SetLegIntent.name, '{"leg_index": 0, "intent": "unpaved"}')
        assert (await kit.store.get(SLUG)).legs[0].intent is LegIntent.UNPAVED

    async def test_it_reroutes_the_leg(self):
        """Changing the mode changes the road, so the geometry has to be recomputed."""
        router = StubRouter()
        kit = await tools(router=router)
        await call(kit, SetLegIntent.name, '{"leg_index": 0, "intent": "unpaved"}')
        assert router.calls

    async def test_an_unknown_intent_is_refused(self):
        kit = await tools()
        with pytest.raises(ToolCallFailed):
            await call(kit, SetLegIntent.name, '{"leg_index": 0, "intent": "hover"}')

    async def test_an_unknown_leg_is_refused(self):
        kit = await tools()
        with pytest.raises(ToolCallFailed):
            await call(kit, SetLegIntent.name, '{"leg_index": 7, "intent": "unpaved"}')


class TestAddPoiToRoute:
    """M1 item five, and the root document names it as the test of the both-paths rule."""

    @staticmethod
    def _poi() -> Poi:
        return Poi(
            id="poi-1",
            name="Halfway Flat",
            category=PoiCategory.WILD_CAMP,
            coordinate=coordinate(47.05, -121.05),
            source=PoiSource.PLACES,
            place_id="ChIJ_halfway",
        )

    async def test_it_adds_the_poi_as_a_waypoint(self):
        kit = await tools(document=trip(pois=(self._poi(),)))
        await call(kit, AddPoiToRoute.name, '{"place_id": "ChIJ_halfway"}')
        assert len((await kit.store.get(SLUG)).waypoints) == 3

    async def test_a_place_not_on_the_trip_is_refused(self):
        """Takes a place already resolved onto the trip, never a coordinate — so there is no
        path from model output to the map without Places having agreed the place exists."""
        kit = await tools(document=trip(pois=(self._poi(),)))
        with pytest.raises(ToolCallFailed):
            await call(kit, AddPoiToRoute.name, '{"place_id": "ChIJ_invented"}')

    async def test_it_routes_before_saving(self):
        router = StubRouter()
        kit = await tools(document=trip(pois=(self._poi(),)), router=router)
        await call(kit, AddPoiToRoute.name, '{"place_id": "ChIJ_halfway"}')
        assert router.calls


class TestFindPlaces:
    """Tim's own example — "find me more restaurants on the route" — so it has to work."""

    async def test_it_runs_the_same_pipeline_replan_runs(self):
        kit = await tools(document=trip(routed=True), discovery=_StubDiscovery())
        outcome = await call(kit, FindPlaces.name, '{"categories": ["food"]}')
        assert outcome.found >= 1

    async def test_the_found_places_are_saved_to_the_trip(self):
        kit = await tools(document=trip(routed=True), discovery=_StubDiscovery())
        await call(kit, FindPlaces.name, '{"categories": ["food"]}')
        assert (await kit.store.get(SLUG)).pois

    async def test_it_says_so_when_discovery_is_not_configured(self):
        """Four API keys, and a backend without them still serves every other tool."""
        kit = await tools(document=trip(routed=True), discovery=None)
        with pytest.raises(ToolCallFailed):
            await call(kit, FindPlaces.name, '{"categories": ["food"]}')

    async def test_an_unknown_category_is_refused(self):
        kit = await tools(document=trip(routed=True), discovery=_StubDiscovery())
        with pytest.raises(ToolCallFailed):
            await call(kit, FindPlaces.name, '{"categories": ["haunted_houses"]}')


class _StubDiscovery(DiscoveryPipeline):
    def __init__(self):
        pass

    async def run(self, leg, categories, **kwargs):
        from motorooter.planning.discovery.pipeline import DiscoveryProgress

        yield DiscoveryProgress(
            stage="done",
            message="1 worth showing",
            progress=1.0,
            pois=(
                Poi(
                    id="poi-found",
                    name="A diner",
                    category=PoiCategory.FOOD,
                    coordinate=coordinate(47.05, -121.05),
                    source=PoiSource.PLACES,
                    place_id="ChIJ_diner",
                ),
            ),
        )


class TestTheSetItself:
    async def test_every_tool_is_published(self):
        kit = await tools()
        assert {spec["name"] for spec in kit.registry.specs()} == {
            "describe_trip",
            "find_places",
            "add_waypoint",
            "remove_waypoint",
            "set_riding_mode",
            "set_leg_intent",
            "add_poi_to_route",
        }

    async def test_no_tool_takes_a_free_text_search_string(self):
        """Deliberate: search strings would put model-authored geography upstream of the
        corridor filter rather than downstream of it, which is the hole the four-stage
        pipeline exists to close."""
        kit = await tools()
        for spec in kit.registry.specs():
            properties = spec["parameters"].get("properties", {})
            assert "query" not in properties
            assert "search" not in properties


class TestSurfaceIsReportedInThreeParts:
    """`unknown` is a share in its own right, and the arithmetic belongs to the domain.

    The first version of describe_trip computed the paved share from an attribute that does
    not exist, so `getattr(..., 0.0)` silently returned zero and every surveyed metre of
    tarmac was reported as unsurveyed. A live run did not catch it: the test corridor routed
    through Google, which reports no surface at all, so the wrong answer and the right one
    were the same string.
    """

    @staticmethod
    def _leg_with_spans() -> RouteLeg:
        from motorooter.routing.models import Surface, SurfaceSpan

        # Ten points in a line; the first half tagged paved, the rest untagged.
        geometry = tuple(Coordinate(lat=47.0 + index * 0.01, lon=-121.0) for index in range(11))
        return RouteLeg(
            geometry=geometry,
            distance_m=10_000.0,
            duration_s=900.0,
            provider="ors",
            intent=LegIntent.UNPAVED,
            surface_spans=(SurfaceSpan(start_index=0, end_index=5, surface=Surface.PAVED),),
        )

    async def test_paved_is_not_reported_as_unsurveyed(self):
        leg = self._leg_with_spans()
        kit = await tools(
            document=trip(
                legs=(
                    TripLeg(
                        intent=LegIntent.UNPAVED,
                        start_waypoint_index=0,
                        end_waypoint_index=1,
                        routed=leg,
                    ),
                )
            )
        )
        content = (await call(kit, DescribeTrip.name, "{}")).content
        # Parsed, not substring-matched: "0% paved" also matches inside "0% unpaved", which
        # is how this assertion passed against the broken version the first time.
        shares = {
            word: pct for pct, word in re.findall(r"(\d+)% (unpaved|paved|unsurveyed)", content)
        }
        assert shares["paved"] != "0"
        assert shares["unsurveyed"] != "100"

    async def test_the_three_shares_are_all_present(self):
        kit = await tools(document=trip(routed=True))
        content = (await call(kit, DescribeTrip.name, "{}")).content
        assert "unpaved" in content and "paved" in content and "unsurveyed" in content


class TestAddWaypointTakesANameNotACoordinate:
    """The hole this closes, and why the signature is the fix rather than the prompt.

    `add_waypoint(lat, lon)` was the one place in the system where a model's assertion became
    geometry with nothing in between. Every other stage already refuses that: extraction may
    only name places present in its input, resolution turns a claim into a `place_id` or drops
    it, `add_poi_to_route` takes an id already on the trip.

    Measured before changing it, on a real model: asked to set a start of "Woodinville, WA" it
    supplied a coordinate 30 m from the true one, and the waypoint was pinned. Thirty metres is
    what makes it dangerous rather than reassuring — recall is good for famous towns, so
    nothing looks wrong, and it will be silently wrong for a forest-road junction.

    With no coordinate argument there is nowhere for a fabrication to arrive. "Never invent a
    place" stops being an instruction the model may decline.
    """

    class StubLookup:
        def __init__(self, *found, error=None):
            self.found = found
            self.error = error
            self.asked: list[tuple[str, object]] = []

        async def search(self, text, *, near=None, limit=5):
            self.asked.append((text, near))
            if self.error is not None:
                raise self.error
            return self.found

    @staticmethod
    def _found(name="Leavenworth", place_id="ChIJ_wa", lat=47.596, lon=-120.661, address=None):
        from motorooter.planning.discovery.lookup import FoundPlace

        return FoundPlace(
            name=name,
            place_id=place_id,
            coordinate=Coordinate(lat=lat, lon=lon),
            kinds=("locality",),
            address=address,
        )

    async def test_it_takes_a_name(self):
        lookup = self.StubLookup(self._found())
        kit = await tools(lookup=lookup)
        await call(kit, AddWaypoint.name, '{"name": "Leavenworth"}')
        assert lookup.asked[0][0] == "Leavenworth"

    async def test_the_pinned_coordinate_comes_from_places(self):
        """Not from the model. That is the whole change."""
        kit = await tools(lookup=self.StubLookup(self._found(lat=47.596, lon=-120.661)))
        await call(kit, AddWaypoint.name, '{"name": "Leavenworth"}')
        added = (await kit.store.get(SLUG)).waypoints[-1]
        assert added.coordinate.lat == pytest.approx(47.596)

    async def test_a_coordinate_argument_is_refused(self):
        """There must be no way to smuggle one in."""
        kit = await tools(lookup=self.StubLookup(self._found()))
        with pytest.raises(ToolCallFailed):
            await call(kit, AddWaypoint.name, '{"lat": 47.0, "lon": -121.0}')

    async def test_nothing_found_is_reported_to_the_model(self):
        kit = await tools(lookup=self.StubLookup())
        with pytest.raises(ToolCallFailed):
            await call(kit, AddWaypoint.name, '{"name": "asdfghjkl"}')

    async def test_nothing_is_pinned_when_nothing_is_found(self):
        kit = await tools(lookup=self.StubLookup())
        before = len((await kit.store.get(SLUG)).waypoints)
        with pytest.raises(ToolCallFailed):
            await call(kit, AddWaypoint.name, '{"name": "asdfghjkl"}')
        assert len((await kit.store.get(SLUG)).waypoints) == before


class TestAmbiguityRefusesRatherThanGuesses:
    """Several real places, no basis to choose: hand back the choice.

    The near-bias resolves "Leavenworth" 60 km from an existing waypoint and does nothing on an
    empty trip, which is exactly the first message of a new conversation. Pinning the best
    match there is the original failure one layer up — a plausible coordinate, nothing visibly
    wrong, and a route to Bavaria.

    Choosing among verified candidates is judgement the model may exercise. Producing a
    coordinate is not.
    """

    @staticmethod
    def _two():
        from motorooter.planning.discovery.lookup import FoundPlace

        return (
            FoundPlace(
                name="Leavenworth",
                place_id="ChIJ_wa",
                coordinate=Coordinate(lat=47.596, lon=-120.661),
                address="Leavenworth, WA 98826, USA",
            ),
            FoundPlace(
                name="Leavenworth",
                place_id="ChIJ_ks",
                coordinate=Coordinate(lat=39.311, lon=-94.922),
                address="Leavenworth, KS 66048, USA",
            ),
        )

    async def test_it_pins_nothing_when_ambiguous(self):
        kit = await tools(lookup=TestAddWaypointTakesANameNotACoordinate.StubLookup(*self._two()))
        before = len((await kit.store.get(SLUG)).waypoints)
        with pytest.raises(ToolCallFailed):
            await call(kit, AddWaypoint.name, '{"name": "Leavenworth"}')
        assert len((await kit.store.get(SLUG)).waypoints) == before

    async def test_it_offers_the_candidates(self):
        """With what tells them apart, or the model is choosing blind too."""
        kit = await tools(lookup=TestAddWaypointTakesANameNotACoordinate.StubLookup(*self._two()))
        with pytest.raises(ToolCallFailed) as caught:
            await call(kit, AddWaypoint.name, '{"name": "Leavenworth"}')
        assert "WA" in str(caught.value)
        assert "KS" in str(caught.value)

    async def test_a_place_id_can_be_used_to_settle_it(self):
        """The second call: the model picks one of the verified candidates."""
        kit = await tools(lookup=TestAddWaypointTakesANameNotACoordinate.StubLookup(*self._two()))
        await call(kit, AddWaypoint.name, '{"name": "Leavenworth", "place_id": "ChIJ_ks"}')
        added = (await kit.store.get(SLUG)).waypoints[-1]
        assert added.coordinate.lat == pytest.approx(39.311)

    async def test_an_unknown_place_id_is_refused(self):
        """It must be one of the candidates just offered, not another invention."""
        kit = await tools(lookup=TestAddWaypointTakesANameNotACoordinate.StubLookup(*self._two()))
        with pytest.raises(ToolCallFailed):
            await call(kit, AddWaypoint.name, '{"name": "Leavenworth", "place_id": "ChIJ_no"}')


class TestFindPlacesReportsWhileItRuns:
    """Thirty seconds of "Working" was the chat half of Tim's progress complaint.

    The pipeline already emits fifteen or twenty events; `find_places` consumed the stream and
    kept only the last. Forwarding them is plumbing rather than new machinery — the same
    events the Replan button has always shown.
    """

    async def test_it_reports_each_stage(self):
        kit = await tools(document=trip(routed=True), discovery=_StubDiscovery())
        seen: list[tuple[str, float | None]] = []
        tool = kit.registry.get(FindPlaces.name)
        await tool.run(
            tool.parse('{"categories": ["food"]}'),
            on_progress=lambda message, fraction: seen.append((message, fraction)),
        )
        assert seen, "the stream was consumed and nothing was said"

    async def test_it_passes_the_pipeline_message_through(self):
        kit = await tools(document=trip(routed=True), discovery=_StubDiscovery())
        seen: list[str] = []
        tool = kit.registry.get(FindPlaces.name)
        await tool.run(
            tool.parse('{"categories": ["food"]}'),
            on_progress=lambda message, fraction: seen.append(message),
        )
        assert any("worth showing" in message for message in seen)

    async def test_it_still_works_without_a_callback(self):
        """Every other caller, including the REST endpoint's own path."""
        kit = await tools(document=trip(routed=True), discovery=_StubDiscovery())
        outcome = await call(kit, FindPlaces.name, '{"categories": ["food"]}')
        assert outcome.found >= 1
