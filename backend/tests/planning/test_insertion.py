"""Putting a found place into the waypoint list at the point the rider would meet it.

The reason this needs a module of its own: `add_poi_to_route` appends. For one place that
is survivable and for four it is nonsense — a cafe two kilometres from the start becomes the
destination, and the route goes out to the mountains and back for lunch.

The invariant everything here protects is that **the first and last waypoints are the trip**.
A discovered place is a via-point, never an endpoint, however well it scored.
"""

from math import cos, pi, radians

import pytest

from motorooter.planning.insertion import SAME_PLACE_M, insert_in_route_order
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate
from motorooter.trips.models import Waypoint

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180


def north(metres: float, *, east: float = 0.0) -> Coordinate:
    lat = metres / M_PER_DEGREE_LAT
    lon = east / (M_PER_DEGREE_LAT * cos(radians(lat)))
    return Coordinate(lat=lat, lon=lon)


def at(metres: float, *, east: float = 0.0, name: str | None = None) -> Waypoint:
    return Waypoint(coordinate=north(metres, east=east), name=name)


def straight(length_m: float, points: int = 21) -> tuple[Coordinate, ...]:
    step = length_m / (points - 1)
    return tuple(north(step * index) for index in range(points))


ROUTE = straight(10_000)
ENDS = (at(0, name="start"), at(10_000, name="end"))


def names(waypoints: tuple[Waypoint, ...]) -> list[str | None]:
    return [point.name for point in waypoints]


class TestWhereItGoes:
    def test_a_place_in_the_middle_lands_in_the_middle(self):
        result = insert_in_route_order(ENDS, [at(5000, east=200, name="cafe")], geometry=ROUTE)
        assert names(result) == ["start", "cafe", "end"]

    def test_several_places_keep_the_order_the_rider_meets_them(self):
        found = [
            at(8000, east=100, name="late"),
            at(2000, east=100, name="early"),
            at(5000, east=100, name="middle"),
        ]
        result = insert_in_route_order(ENDS, found, geometry=ROUTE)
        assert names(result) == ["start", "early", "middle", "late", "end"]

    def test_it_goes_between_the_rider_s_own_waypoints_not_after_them(self):
        riders = (at(0, name="start"), at(6000, name="via"), at(10_000, name="end"))
        result = insert_in_route_order(riders, [at(3000, east=100, name="cafe")], geometry=ROUTE)
        assert names(result) == ["start", "cafe", "via", "end"]

    def test_a_place_past_the_rider_s_via_goes_after_it(self):
        riders = (at(0, name="start"), at(3000, name="via"), at(10_000, name="end"))
        result = insert_in_route_order(riders, [at(7000, east=100, name="camp")], geometry=ROUTE)
        assert names(result) == ["start", "via", "camp", "end"]

    def test_ordering_follows_the_road_not_the_crow(self):
        """A route that doubles back meets the far arm last, though it passes nearer home.

        Chosen so the two orderings disagree: the far-arm place is 3.5 km from the start in
        a straight line and the near-arm place 5 km, so sorting by distance-from-start would
        put them the wrong way round. Along the road it is 5 km against 18 km.
        """
        out = tuple(north(step) for step in range(0, 10_001, 1000))
        back = tuple(
            Coordinate(
                lat=north(10_000 - step).lat,
                lon=north(0, east=step * 3000 / 10_000).lon,
            )
            for step in range(0, 10_001, 1000)
        )
        riders = (
            at(0, name="start"),
            Waypoint(coordinate=north(0, east=3000), name="end"),
        )
        found = [
            Waypoint(coordinate=north(2000, east=2450), name="on the far arm"),
            at(5000, east=60, name="on the near arm"),
        ]
        result = insert_in_route_order(riders, found, geometry=(*out, *back))
        assert names(result) == ["start", "on the near arm", "on the far arm", "end"]


class TestWhatItRefusesToDo:
    def test_the_destination_stays_the_destination(self):
        """A place beyond the end of the route still goes before it."""
        result = insert_in_route_order(ENDS, [at(20_000, name="miles past")], geometry=ROUTE)
        assert names(result) == ["start", "miles past", "end"]

    def test_the_start_stays_the_start(self):
        result = insert_in_route_order(ENDS, [at(-5000, name="behind")], geometry=ROUTE)
        assert names(result) == ["start", "behind", "end"]

    def test_a_place_the_route_already_stops_at_is_not_added_twice(self):
        riders = (at(0, name="start"), at(5000, name="already here"), at(10_000, name="end"))
        result = insert_in_route_order(riders, [at(5000, name="same spot")], geometry=ROUTE)
        assert names(result) == ["start", "already here", "end"]

    def test_two_found_places_at_the_same_spot_only_one_is_added(self):
        found = [at(5000, name="first"), at(5000, east=SAME_PLACE_M / 2, name="second")]
        result = insert_in_route_order(ENDS, found, geometry=ROUTE)
        assert names(result) == ["start", "first", "end"]

    def test_a_place_just_outside_the_same_spot_radius_is_added(self):
        riders = (at(0, name="start"), at(5000, name="via"), at(10_000, name="end"))
        found = [at(5000, east=SAME_PLACE_M * 3, name="nearby")]
        result = insert_in_route_order(riders, found, geometry=ROUTE)
        assert "nearby" in names(result)


class TestNothingToDo:
    def test_no_additions_leaves_the_trip_alone(self):
        assert insert_in_route_order(ENDS, [], geometry=ROUTE) == ENDS

    def test_without_geometry_the_endpoints_are_still_protected(self):
        """Ordering is unknowable, but making a cafe the destination is knowably wrong."""
        result = insert_in_route_order(ENDS, [at(5000, name="cafe")], geometry=())
        assert names(result) == ["start", "cafe", "end"]

    def test_a_trip_with_one_waypoint_gains_the_place_after_it(self):
        result = insert_in_route_order((at(0, name="start"),), [at(5000, name="cafe")], geometry=())
        assert names(result) == ["start", "cafe"]

    def test_a_trip_with_no_waypoints_is_just_the_places(self):
        result = insert_in_route_order((), [at(5000, name="cafe")], geometry=ROUTE)
        assert names(result) == ["cafe"]


class TestWhatItPreserves:
    def test_the_rider_s_waypoints_come_through_untouched(self):
        riders = (
            Waypoint(coordinate=north(0), name="start", pinned=True),
            Waypoint(coordinate=north(6000), name="shaping", pinned=False),
            Waypoint(coordinate=north(10_000), name="end", pinned=True),
        )
        result = insert_in_route_order(riders, [at(3000, name="cafe")], geometry=ROUTE)
        assert [point for point in result if point.name != "cafe"] == list(riders)

    def test_an_added_place_keeps_its_name_and_coordinate(self):
        cafe = at(5000, east=200, name="Cottage Cafe")
        result = insert_in_route_order(ENDS, [cafe], geometry=ROUTE)
        assert result[1] == cafe


class TestSamePlaceRadius:
    def test_it_is_small_enough_not_to_swallow_a_real_neighbour(self):
        """Two distinct cafes across a street must both be addable."""
        assert SAME_PLACE_M <= 100.0

    def test_it_is_large_enough_to_catch_places_pinned_from_different_sources(self):
        """Places and a map click will not agree to the metre on the same building."""
        assert SAME_PLACE_M >= 10.0

    @pytest.mark.parametrize("offset", [0.0, 1.0, 5.0])
    def test_a_place_within_it_is_the_same_place(self, offset):
        riders = (at(0, name="start"), at(5000, name="via"), at(10_000, name="end"))
        result = insert_in_route_order(riders, [at(5000, east=offset, name="dup")], geometry=ROUTE)
        assert "dup" not in names(result)
