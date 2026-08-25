"""Route metrics computed from geometry.

These exist so the discovery scorer is handed numbers rather than asked to estimate them. A
model asked "how twisty is this road" will answer confidently and be unfalsifiable; a model
handed "412 degrees of heading change per kilometre" is being asked the question it is
actually good at — is that *worth riding* — and the number itself is testable like any other
function.

The hard part is noise. Route geometry is densely sampled and slightly jittery, so a
straight highway is not a straight line: it is two thousand nearly-collinear points, and
summing heading changes across them measures the sampling, not the road.
"""

from math import cos, pi, radians

import pytest

from motorooter.planning.metrics import (
    MIN_HEADING_SEGMENT_M,
    bearing_deg,
    detour_ratio,
    nearest_distance_m,
    twistiness_deg_per_km,
)
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180


def north(metres: float, *, east: float = 0.0) -> Coordinate:
    lat = metres / M_PER_DEGREE_LAT
    lon = east / (M_PER_DEGREE_LAT * cos(radians(lat)))
    return Coordinate(lat=lat, lon=lon)


def straight(length_m: float, points: int) -> tuple[Coordinate, ...]:
    step = length_m / (points - 1)
    return tuple(north(step * index) for index in range(points))


class TestBearing:
    def test_due_north_is_zero(self):
        assert bearing_deg(north(0), north(1000)) == pytest.approx(0.0, abs=1e-6)

    def test_due_east_is_ninety(self):
        assert bearing_deg(north(0), north(0, east=1000)) == pytest.approx(90.0, abs=0.1)

    def test_due_south_is_one_eighty(self):
        assert bearing_deg(north(1000), north(0)) == pytest.approx(180.0, abs=1e-6)

    def test_it_is_reported_in_zero_to_three_sixty(self):
        westward = bearing_deg(north(0), north(0, east=-1000))
        assert 0.0 <= westward < 360.0
        assert westward == pytest.approx(270.0, abs=0.1)


class TestTwistiness:
    """Summed absolute heading change per kilometre."""

    def test_a_straight_road_is_not_twisty(self):
        assert twistiness_deg_per_km(straight(10_000, 50)) == pytest.approx(0.0, abs=1e-6)

    def test_a_right_angle_contributes_ninety_degrees(self):
        corner = (north(0), north(1000), north(1000, east=1000))
        assert twistiness_deg_per_km(corner) == pytest.approx(90.0 / 2.0, rel=1e-3)

    def test_a_switchback_counts_the_full_reversal(self):
        """A hairpin is the most twisty thing a road does; it must not cancel out.

        Nearly 180 degrees of turn packed into 400 m of road, which is what makes the
        per-kilometre figure large — the reversal itself is only worth 180 degrees.
        """
        hairpin = (north(0), north(200), north(0, east=10))
        assert twistiness_deg_per_km(hairpin) > 400.0

    def test_turning_left_and_right_do_not_cancel(self):
        """Signed heading change would report an S-bend as straight."""
        s_bend = (
            north(0),
            north(1000),
            north(1000, east=1000),
            north(2000, east=1000),
        )
        assert twistiness_deg_per_km(s_bend) == pytest.approx(180.0 / 3.0, rel=1e-3)

    def test_crossing_north_does_not_report_a_huge_turn(self):
        """359 degrees to 1 degree is a 2 degree turn, not a 358 degree one."""
        geometry = (north(0, east=-20), north(1000), north(2000, east=20))
        assert twistiness_deg_per_km(geometry) < 10.0

    def test_a_twisty_road_scores_above_a_straight_one(self):
        assert twistiness_deg_per_km(
            (north(0), north(500, east=300), north(1000), north(1500, east=-300), north(2000))
        ) > twistiness_deg_per_km(straight(2000, 5))

    def test_it_is_independent_of_sampling_density(self):
        """The same corner sampled twice as finely is the same corner."""
        coarse = (north(0), north(2000), north(2000, east=2000))
        fine = (
            north(0),
            north(1000),
            north(2000),
            north(2000, east=1000),
            north(2000, east=2000),
        )
        assert twistiness_deg_per_km(coarse) == pytest.approx(twistiness_deg_per_km(fine), rel=1e-3)

    def test_it_is_per_kilometre_not_a_total(self):
        """Otherwise a long straight road outscores a short mountain pass."""
        one_corner_short = (north(0), north(500), north(500, east=500))
        one_corner_long = (north(0), north(5000), north(5000, east=5000))
        assert twistiness_deg_per_km(one_corner_short) > twistiness_deg_per_km(one_corner_long)


class TestTwistinessIsNotFooledByNoise:
    """The measurement that would otherwise be meaningless on real geometry."""

    def test_jitter_on_a_straight_road_does_not_read_as_twisty(self):
        """Sub-metre wobble between densely sampled points is sampling, not corners.

        This is why the threshold has to exceed the sampling interval rather than merely
        exceed the jitter: at 15 m it never engaged on 20 m sampling, and this road — which
        is straight to within 5 cm — scored 29 deg/km, a third of a right-angle bend
        per kilometre.
        """
        jittered = tuple(
            north(index * 20.0, east=0.05 if index % 2 else -0.05) for index in range(500)
        )
        assert twistiness_deg_per_km(jittered) < 5.0

    def test_the_threshold_is_tunable(self):
        """It is a guess, so it must be adjustable without editing the function."""
        jittered = tuple(
            north(index * 20.0, east=0.05 if index % 2 else -0.05) for index in range(200)
        )
        assert twistiness_deg_per_km(jittered, min_segment_m=5.0) > twistiness_deg_per_km(
            jittered, min_segment_m=200.0
        )

    def test_segments_below_the_threshold_are_skipped(self):
        """Two points a centimetre apart have a bearing, and it is noise."""
        nudged = (north(0), north(0.01), north(10_000))
        assert twistiness_deg_per_km(nudged) == pytest.approx(0.0, abs=1e-6)

    def test_a_real_corner_survives_the_filter(self):
        """The filter must not flatten the thing being measured."""
        corner = (
            north(0),
            north(MIN_HEADING_SEGMENT_M * 5),
            north(MIN_HEADING_SEGMENT_M * 5, east=1000),
        )
        assert twistiness_deg_per_km(corner) > 0.0


class TestDegenerateGeometry:
    @pytest.mark.parametrize("geometry", [(), (Coordinate(lat=0, lon=0),)])
    def test_too_few_points_is_zero_not_an_error(self, geometry):
        """An unrouted or single-point leg is a legitimate state."""
        assert twistiness_deg_per_km(geometry) == 0.0

    def test_a_zero_length_route_is_zero_not_a_division_by_zero(self):
        same = Coordinate(lat=45.0, lon=-121.0)
        assert twistiness_deg_per_km((same, same, same)) == 0.0

    def test_detour_ratio_of_a_zero_length_route_is_one(self):
        same = Coordinate(lat=45.0, lon=-121.0)
        assert detour_ratio((same, same)) == 1.0


class TestDetourRatio:
    """How far the road wanders relative to the straight line between its ends."""

    def test_a_straight_route_is_one(self):
        assert detour_ratio(straight(10_000, 20)) == pytest.approx(1.0, rel=1e-6)

    def test_a_dogleg_is_longer_than_the_direct_line(self):
        dogleg = (north(0), north(1000, east=1000), north(2000))
        assert detour_ratio(dogleg) == pytest.approx(2**0.5, rel=1e-3)

    def test_a_loop_returning_to_its_start_is_unbounded_not_infinite(self):
        """A round trip has no meaningful direct line; it must not divide by zero."""
        loop = (north(0), north(1000), north(1000, east=1000), north(0, east=0))
        assert detour_ratio(loop) > 1.0

    def test_it_ignores_the_shape_between_the_ends(self):
        """Two routes of equal length between the same points detour equally."""
        wiggly = (north(0), north(500, east=100), north(1000, east=-100), north(1500))
        direct_len = detour_ratio((north(0), north(1500)))
        assert detour_ratio(wiggly) > direct_len


class TestNearestDistance:
    """How far a candidate sits off the route — the input to "is the detour worth it"."""

    def test_a_point_on_the_route_is_zero_away(self):
        route = straight(10_000, 11)
        assert nearest_distance_m(route, north(5000)) == pytest.approx(0.0, abs=1.0)

    def test_a_point_beside_the_route_measures_the_perpendicular(self):
        route = straight(10_000, 11)
        assert nearest_distance_m(route, north(5000, east=300)) == pytest.approx(300.0, rel=0.02)

    def test_it_measures_to_the_segment_not_only_to_the_vertices(self):
        """A point beside the middle of a long segment is close to the road, not to a vertex."""
        route = (north(0), north(10_000))
        assert nearest_distance_m(route, north(5000, east=100)) == pytest.approx(100.0, rel=0.05)

    def test_a_point_beyond_the_end_measures_to_the_end(self):
        route = (north(0), north(1000))
        assert nearest_distance_m(route, north(2000)) == pytest.approx(1000.0, rel=0.02)

    def test_it_takes_the_closest_of_several_segments(self):
        route = (north(0), north(10_000), north(10_000, east=10_000))
        assert nearest_distance_m(
            route,
            north(
                10_000,
                east=5000,
            ),
        ) == pytest.approx(0.0, abs=5.0)

    def test_an_empty_route_has_no_distance(self):
        assert nearest_distance_m((), north(0)) is None

    def test_a_single_point_route_still_measures(self):
        assert nearest_distance_m((north(0),), north(1000)) == pytest.approx(1000.0, rel=0.02)
