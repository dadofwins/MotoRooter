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
    TripTools,
)
from motorooter.llm.errors import ToolCallFailed
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.routing.errors import NoRouteFound
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import Poi, PoiCategory, PoiSource, Trip, TripLeg, Waypoint, utc_now
from motorooter.trips.store import InMemoryTripStore

SLUG = "cascade-loop"


def coordinate(lat: float = 47.0, lon: float = -121.0) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


def trip(*, waypoints=None, legs=None, pois=(), routed=False) -> Trip:
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


async def tools(*, document: Trip | None = None, router=None, discovery=None) -> TripTools:
    store = await store_with(document or trip())
    return TripTools(store=store, slug=SLUG, router=router or StubRouter(), discovery=discovery)


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


class TestAddWaypoint:
    async def test_it_routes_before_it_saves(self):
        """A coordinate from a model is a claim until a router agrees with it."""
        router = StubRouter()
        kit = await tools(router=router)
        await call(kit, AddWaypoint.name, '{"lat": 47.05, "lon": -121.05}')
        assert router.calls, "saved without routing"

    async def test_an_unroutable_point_is_not_saved(self):
        """The failure that must not become a pin: the model invents somewhere in the sea."""
        router = StubRouter(error=NoRouteFound("no road within 350 m"))
        kit = await tools(router=router)
        with pytest.raises(ToolCallFailed):
            await call(kit, AddWaypoint.name, '{"lat": 0.0, "lon": 0.0}')
        assert len((await kit.store.get(SLUG)).waypoints) == 2

    async def test_the_waypoint_is_added(self):
        kit = await tools()
        await call(kit, AddWaypoint.name, '{"lat": 47.05, "lon": -121.05}')
        assert len((await kit.store.get(SLUG)).waypoints) == 3

    async def test_it_returns_the_numbered_list(self):
        """Indices shift under every edit, including the rider's. A prose summary leaves the
        model holding a number that now means a different waypoint."""
        kit = await tools()
        outcome = await call(kit, AddWaypoint.name, '{"lat": 47.05, "lon": -121.05}')
        assert "0" in outcome.content and "2" in outcome.content

    async def test_a_bad_coordinate_is_the_models_mistake(self):
        kit = await tools()
        with pytest.raises(ToolCallFailed):
            await call(kit, AddWaypoint.name, '{"lat": 200.0, "lon": -121.0}')


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
