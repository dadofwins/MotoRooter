"""Routing a trip through the best of what discovery found.

The one service function behind both the button and the assistant's tool. Everything it does
is an autonomous edit to something the rider built, so the tests below are mostly about what
it *refuses* to do and about whether the rider can tell what happened.
"""

from datetime import UTC, datetime
from math import cos, pi, radians

import pytest

from motorooter.planning.route_through import route_through_best
from motorooter.routing.errors import ProviderUnavailable, RouteIncomplete
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    RouteLeg,
    Surface,
    SurfaceSpan,
)
from motorooter.trips.models import Poi, PoiCategory, PoiSource, Trip, TripLeg, Waypoint
from motorooter.trips.store import InMemoryTripStore

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180
LENGTH_M = 400_000.0
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def north(metres: float, *, east: float = 0.0) -> Coordinate:
    lat = metres / M_PER_DEGREE_LAT
    lon = east / (M_PER_DEGREE_LAT * cos(radians(lat)))
    return Coordinate(lat=lat, lon=lon)


def routed(length_m: float = LENGTH_M) -> RouteLeg:
    points = tuple(north(length_m * index / 100) for index in range(101))
    return RouteLeg(
        geometry=points,
        distance_m=length_m,
        duration_s=0.0,
        surface_spans=(
            SurfaceSpan(
                surface=Surface.PAVED,
                distance_m=length_m,
                start_index=0,
                end_index=len(points) - 1,
            ),
        ),
        provider="fake",
        intent=LegIntent.TWISTY_PAVED,
    )


def poi(name: str, score: float | None, *, along: float = 0.5, off_route_m: float = 100.0) -> Poi:
    return Poi(
        id=f"poi-{name}",
        name=name,
        category=PoiCategory.VIEWPOINT,
        coordinate=north(LENGTH_M * along, east=off_route_m),
        source=PoiSource.PLACES,
        place_id=f"ChIJ-{name}",
        score=score,
    )


def trip(*pois: Poi, legs: tuple[TripLeg, ...] | None = None) -> Trip:
    return Trip(
        slug="wabdr",
        name="WABDR",
        created_at=T0,
        edited_at=T0,
        waypoints=(
            Waypoint(coordinate=north(0), name="start"),
            Waypoint(coordinate=north(LENGTH_M), name="end"),
        ),
        legs=(
            legs
            if legs is not None
            else (
                TripLeg(
                    intent=LegIntent.TWISTY_PAVED,
                    start_waypoint_index=0,
                    end_waypoint_index=1,
                    routed=routed(),
                ),
            )
        ),
        pois=pois,
    )


def long_trip(*pois: Poi) -> Trip:
    """Six waypoints, so "one stretch" and "the whole trip" are visibly different."""
    points = tuple(
        Waypoint(coordinate=north(LENGTH_M * index / 5), name=f"w{index}") for index in range(6)
    )
    return Trip(
        slug="wabdr",
        name="WABDR",
        created_at=T0,
        edited_at=T0,
        waypoints=points,
        legs=tuple(
            TripLeg(
                intent=LegIntent.TWISTY_PAVED,
                start_waypoint_index=index,
                end_waypoint_index=index + 1,
                routed=routed(),
            )
            for index in range(5)
        ),
        pois=pois,
    )


class FakeRouter:
    """Records what it was asked to join, and can be told to refuse."""

    def __init__(self, *, fails: bool = False) -> None:
        self.calls: list[tuple[Waypoint, ...]] = []
        self._fails = fails

    async def route_waypoints(self, waypoints, *, intent, provider_override=None):
        self.calls.append(tuple(waypoints))
        if self._fails:
            raise ProviderUnavailable("provider down")
        return (routed(),)


async def store_with(saved: Trip) -> InMemoryTripStore:
    store = InMemoryTripStore()
    await store.put(saved)
    return store


def rerouted(saved: Trip) -> Trip:
    """`saved` with geometry back on every leg, as the map would leave it."""
    return saved.model_copy(
        update={"legs": tuple(leg.model_copy(update={"routed": routed()}) for leg in saved.legs)}
    )


def names(pois):
    return [place.name for place in pois]


class TestWhatItAdds:
    async def test_it_routes_through_a_place_the_judge_rated_highly(self):
        store = await store_with(trip(poi("Lion Rock", 0.95)))
        result = await route_through_best(store=store, slug="wabdr", router=FakeRouter())
        assert names(result.added) == ["Lion Rock"]
        assert [point.name for point in result.trip.waypoints] == ["start", "Lion Rock", "end"]

    async def test_it_leaves_a_place_the_judge_hedged_about(self):
        store = await store_with(trip(poi("meh", 0.4)))
        result = await route_through_best(store=store, slug="wabdr", router=FakeRouter())
        assert result.added == ()
        assert result.trip.waypoints == (await store.get("wabdr")).waypoints

    async def test_the_places_it_adds_are_marked_as_on_the_route(self):
        """Otherwise the next run adds them again, and the map cannot show which are pinned."""
        store = await store_with(trip(poi("Lion Rock", 0.95)))
        result = await route_through_best(store=store, slug="wabdr", router=FakeRouter())
        assert [place.on_route for place in result.trip.pois] == [True]

    async def test_running_it_twice_does_not_add_the_same_place_twice(self):
        """The on-route flag is what stops it, so the second run must see a routed trip.

        An edit leaves every leg it touched without geometry — they are stale by
        definition — so the map re-routes before anything asks again. `rerouted` stands in
        for that.
        """
        store = await store_with(trip(poi("Lion Rock", 0.95)))
        first = await route_through_best(store=store, slug="wabdr", router=FakeRouter())
        await store.put(rerouted(first.trip))
        again = await route_through_best(store=store, slug="wabdr", router=FakeRouter())
        assert again.added == ()
        assert len(again.trip.waypoints) == 3

    async def test_it_says_what_it_left_out_so_the_rider_can_ask_for_more(self):
        found = [poi(f"p{index}", 0.9 - index / 100, along=0.1 * index) for index in range(5)]
        store = await store_with(trip(*found))
        result = await route_through_best(store=store, slug="wabdr", router=FakeRouter(), limit=2)
        assert len(result.added) == 2
        assert len(result.left_out) == 3

    async def test_the_places_it_added_are_reported_in_route_order(self):
        """The reply reads as a ride, not as a leaderboard."""
        found = [poi("late", 0.8, along=0.8), poi("early", 0.95, along=0.2)]
        store = await store_with(trip(*found))
        result = await route_through_best(store=store, slug="wabdr", router=FakeRouter(), limit=2)
        assert names(result.added) == ["early", "late"]


class TestWhatItRefuses:
    async def test_an_unrouted_trip_has_no_corridor_to_choose_from(self):
        legs = (
            TripLeg(intent=LegIntent.TWISTY_PAVED, start_waypoint_index=0, end_waypoint_index=1),
        )
        store = await store_with(trip(poi("Lion Rock", 0.95), legs=legs))
        with pytest.raises(RouteIncomplete):
            await route_through_best(store=store, slug="wabdr", router=FakeRouter())

    async def test_nothing_is_saved_when_the_new_route_will_not_join(self):
        """Routing is the check on geography, and it happens before the write."""
        store = await store_with(trip(poi("Lion Rock", 0.95)))
        with pytest.raises(ProviderUnavailable):
            await route_through_best(store=store, slug="wabdr", router=FakeRouter(fails=True))
        assert len((await store.get("wabdr")).waypoints) == 2

    async def test_adding_nothing_does_not_touch_the_trip(self):
        """A no-op must not bump `edited_at` and mark discovery stale for no reason."""
        store = await store_with(trip(poi("meh", 0.4)))
        before = await store.get("wabdr")
        await route_through_best(store=store, slug="wabdr", router=FakeRouter())
        assert (await store.get("wabdr")).edited_at == before.edited_at

    async def test_it_confirms_the_stretches_it_creates_before_saving_them(self):
        """Two, for one insertion: into the place, and out of it.

        Not the whole trip as one span. That cost a request the size of the route on every
        edit, and refused edits for failures in stretches nobody had touched.
        """
        router = FakeRouter()
        store = await store_with(trip(poi("Lion Rock", 0.95)))
        await route_through_best(store=store, slug="wabdr", router=router)
        assert [[point.name for point in call] for call in router.calls] == [
            ["start", "Lion Rock"],
            ["Lion Rock", "end"],
        ]

    async def test_a_long_trip_is_not_re_routed_end_to_end_to_add_one_place(self):
        store = await store_with(long_trip(poi("Lion Rock", 0.95, along=0.55)))
        router = FakeRouter()
        await route_through_best(store=store, slug="wabdr", router=router)
        assert [len(call) for call in router.calls] == [2, 2]


class TestTheLegItChooses:
    async def test_it_searches_the_longest_leg_not_the_first(self):
        """A trip that opens with a short connector would otherwise budget against it.

        The budget and the count both come from the leg, so choosing a 2 km connector would
        allow a 300 m detour and one addition on a trip with 400 km of riding behind it.
        """
        connector = routed(2_000.0)
        main = routed(LENGTH_M)
        saved = Trip(
            slug="wabdr",
            name="WABDR",
            created_at=T0,
            edited_at=T0,
            waypoints=(
                Waypoint(coordinate=north(0), name="start"),
                Waypoint(coordinate=north(2_000), name="junction"),
                Waypoint(coordinate=north(LENGTH_M), name="end"),
            ),
            legs=(
                TripLeg(
                    intent=LegIntent.TWISTY_PAVED,
                    start_waypoint_index=0,
                    end_waypoint_index=1,
                    routed=connector,
                ),
                TripLeg(
                    intent=LegIntent.TWISTY_PAVED,
                    start_waypoint_index=1,
                    end_waypoint_index=2,
                    routed=main,
                ),
            ),
            pois=(poi("Lion Rock", 0.95, along=0.5, off_route_m=5_000.0),),
        )
        store = await store_with(saved)
        result = await route_through_best(store=store, slug="wabdr", router=FakeRouter())
        assert names(result.added) == ["Lion Rock"]
