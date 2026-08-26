"""The stretch of road discovery searches along.

Tim ran a four-leg, 495-mile trip and got places for the first half only. Discovery searched
one leg and called it the corridor.

That rule was correct for the world it was written in — a trip was one leg spanning every
waypoint, and the alternative was the assistant's version, which took leg zero and discovered
places around the rider's driveway. Multi-leg landed and falsified it: four comparable legs
means the longest is a quarter of the map.

Costed before it was built, on Tim's own trip:

    today (longest leg, 272 km)   12 anchors   4.4 anchors/100 km
    whole route      (797 km)     33 anchors   4.1 anchors/100 km

2.8x the anchors for 2.9x the route, so the per-kilometre cost is flat. It is not a bigger
search, it is the end of an under-count.
"""

from datetime import UTC, datetime
from math import pi

from motorooter.planning.stitching import search_corridor
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    RouteLeg,
    Surface,
    SurfaceSpan,
)
from motorooter.trips.models import Trip, TripLeg, Waypoint

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180
T0 = datetime(2026, 8, 26, tzinfo=UTC)


def north(metres: float) -> Coordinate:
    return Coordinate(lat=metres / M_PER_DEGREE_LAT, lon=0.0)


def leg(start_m: float, end_m: float, *, surface: Surface | None = None) -> RouteLeg:
    points = tuple(north(start_m + (end_m - start_m) * index / 20) for index in range(21))
    spans = (
        (SurfaceSpan(surface=surface, distance_m=0.0, start_index=0, end_index=20),)
        if surface is not None
        else ()
    )
    return RouteLeg(
        geometry=points,
        distance_m=end_m - start_m,
        duration_s=0.0,
        surface_spans=spans,
        provider="fake",
        intent=LegIntent.UNPAVED,
    )


def trip(*legs: RouteLeg | None) -> Trip:
    points = tuple(
        Waypoint(coordinate=north(index * 100_000), name=f"w{index}")
        for index in range(len(legs) + 1)
    )
    return Trip(
        slug="t",
        name="T",
        created_at=T0,
        edited_at=T0,
        waypoints=points,
        legs=tuple(
            TripLeg(
                intent=LegIntent.UNPAVED,
                start_waypoint_index=index,
                end_waypoint_index=index + 1,
                routed=routed,
            )
            for index, routed in enumerate(legs)
        ),
    )


class TestItCoversTheWholeRide:
    def test_a_four_leg_trip_is_searched_end_to_end(self):
        legs = [leg(index * 100_000, (index + 1) * 100_000) for index in range(4)]
        corridor = search_corridor(trip(*legs))
        assert corridor is not None
        assert corridor.geometry[0] == legs[0].geometry[0]
        assert corridor.geometry[-1] == legs[-1].geometry[-1]

    def test_the_short_leg_is_not_what_gets_searched(self):
        """The failure the old rule replaced, and the one it replaced it with."""
        connector = leg(0.0, 2_000.0)
        main = leg(2_000.0, 400_000.0)
        corridor = search_corridor(trip(connector, main))
        assert corridor is not None
        assert corridor.geometry[0] == connector.geometry[0]
        assert corridor.geometry[-1] == main.geometry[-1]

    def test_a_single_leg_trip_is_unchanged(self):
        only = leg(0.0, 100_000.0)
        corridor = search_corridor(trip(only))
        assert corridor is not None
        assert corridor.geometry == only.geometry

    def test_surface_spans_survive_the_join(self):
        """Evidence reads surface off the corridor, so a join that drops spans is silent."""
        legs = [
            leg(0.0, 100_000.0, surface=Surface.UNPAVED),
            leg(100_000.0, 200_000.0, surface=Surface.PAVED),
        ]
        corridor = search_corridor(trip(*legs))
        assert corridor is not None
        assert {span.surface for span in corridor.surface_spans} == {
            Surface.UNPAVED,
            Surface.PAVED,
        }


class TestWhatItSkips:
    def test_an_unrouted_leg_is_left_out_rather_than_breaking_the_corridor(self):
        first, last = leg(0.0, 100_000.0), leg(200_000.0, 300_000.0)
        corridor = search_corridor(trip(first, None, last))
        assert corridor is not None
        assert corridor.geometry[0] == first.geometry[0]
        assert corridor.geometry[-1] == last.geometry[-1]

    def test_a_trip_with_nothing_routed_has_no_corridor(self):
        assert search_corridor(trip(None, None)) is None

    def test_a_trip_with_no_legs_has_no_corridor(self):
        assert search_corridor(trip()) is None
