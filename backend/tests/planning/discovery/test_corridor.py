"""Sampling a route into the anchors discovery searches around.

Discovery cannot search "along a route" — every source it talks to takes a *place*. So the
route is reduced to a series of points, and everything downstream fans out from those.

The spacing is load-bearing twice over. It decides how many searches a corridor costs, and it
is the same quantity M0 identified as a hard product requirement for routing: at endpoint-only
density the engine produces a plausible route down the wrong roads, and 10-15 km spacing is
what reproduced a real BDR. One function, so the two cannot drift apart.
"""

from itertools import pairwise
from math import pi

import pytest

from motorooter.planning.discovery.corridor import (
    DEFAULT_ANCHOR_SPACING_M,
    anchors,
    spacing_of,
)
from motorooter.routing.geo import EARTH_RADIUS_M, haversine_m
from motorooter.routing.models import Coordinate

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180


def north(metres: float) -> Coordinate:
    return Coordinate(lat=metres / M_PER_DEGREE_LAT, lon=0.0)


def straight(length_m: float, points: int = 200) -> tuple[Coordinate, ...]:
    step = length_m / (points - 1)
    return tuple(north(step * index) for index in range(points))


class TestWhereTheAnchorsGo:
    def test_the_route_start_is_always_an_anchor(self):
        """A rider's first stop matters as much as their last."""
        assert anchors(straight(100_000), spacing_m=15_000)[0] == north(0)

    def test_the_route_end_is_always_an_anchor(self):
        route = straight(100_000)
        assert anchors(route, spacing_m=15_000)[-1] == route[-1]

    def test_anchors_are_spaced_at_roughly_the_requested_interval(self):
        placed = anchors(straight(100_000), spacing_m=20_000)
        gaps = [haversine_m(a, b) for a, b in pairwise(placed)]
        # The last gap is a remainder, so it is shorter; the rest should be on interval.
        assert all(gap == pytest.approx(20_000, rel=0.05) for gap in gaps[:-1])

    def test_no_gap_ever_exceeds_the_interval(self):
        """A gap larger than the interval is a stretch of route nobody searched."""
        placed = anchors(straight(97_000), spacing_m=15_000)
        gaps = [haversine_m(a, b) for a, b in pairwise(placed)]
        assert max(gaps) <= 15_000 * 1.05

    def test_a_longer_route_gets_more_anchors(self):
        assert len(anchors(straight(200_000), spacing_m=20_000)) > len(
            anchors(straight(100_000), spacing_m=20_000)
        )

    def test_anchors_lie_on_the_route(self):
        """Interpolated along the polyline, not between distant vertices."""
        route = (north(0), north(50_000), north(100_000))
        for anchor in anchors(route, spacing_m=10_000):
            assert anchor.lon == pytest.approx(0.0, abs=1e-9)

    def test_a_hundred_kilometre_route_at_fifteen_costs_eight_searches(self):
        """Anchor count *is* the search budget, so it should be predictable."""
        assert len(anchors(straight(100_000), spacing_m=15_000)) == 8


class TestTheDensityRequirementFromM0:
    """Endpoint-only routing produces a plausible route down the wrong roads.

    M0 measured it: no intermediate waypoints put 37% of the route within 100 m of the real
    BDR, and eight waypoints put 58% there. This is the same spacing question, so it is the
    same function — a separate implementation for routing would drift from the one discovery
    uses, and the failure is invisible because both produce continuous geometry.
    """

    def test_the_default_sits_in_the_measured_band(self):
        assert 10_000 <= DEFAULT_ANCHOR_SPACING_M <= 15_000

    def test_a_real_length_bdr_section_gets_the_density_m0_needed(self):
        """WABDR Section 3 is about 127 km; M0 wanted eight or more waypoints on it."""
        assert len(anchors(straight(127_000))) >= 8

    def test_measured_spacing_reports_what_was_achieved(self):
        """So a tool's output can be asserted on rather than assumed."""
        placed = anchors(straight(100_000), spacing_m=15_000)
        assert spacing_of(placed) == pytest.approx(100_000 / (len(placed) - 1), rel=0.1)


class TestDegenerateRoutes:
    def test_an_empty_route_has_no_anchors(self):
        assert anchors(()) == ()

    def test_a_single_point_route_is_its_own_anchor(self):
        assert anchors((north(0),)) == (north(0),)

    def test_a_route_shorter_than_the_interval_gets_both_ends(self):
        """Two anchors, not one: a short leg still has a start and an end worth searching."""
        assert len(anchors((north(0), north(500)), spacing_m=15_000)) == 2

    def test_a_zero_length_route_does_not_loop_forever(self):
        same = north(0)
        assert len(anchors((same, same, same), spacing_m=15_000)) <= 2

    def test_spacing_of_a_single_anchor_is_zero(self):
        assert spacing_of((north(0),)) == 0.0


class TestGuardrails:
    @pytest.mark.parametrize("spacing", [0.0, -1.0])
    def test_a_non_positive_spacing_is_refused(self, spacing):
        """Zero would place an unbounded number of anchors, one search each."""
        with pytest.raises(ValueError):
            anchors(straight(10_000), spacing_m=spacing)

    def test_the_anchor_count_is_capped(self):
        """Anchor count is the search budget. An unbounded route must not become an
        unbounded fan-out — both Brave and Places are metered, and discovery multiplies
        every anchor by every category."""
        placed = anchors(straight(10_000_000), spacing_m=1_000, max_anchors=50)
        assert len(placed) == 50

    def test_capping_still_covers_the_whole_route(self):
        """Truncating the tail would silently stop searching two thirds of the trip."""
        route = straight(1_000_000)
        placed = anchors(route, spacing_m=1_000, max_anchors=20)
        assert placed[-1] == route[-1]

    @pytest.mark.parametrize("cap", [0, 1, -1])
    def test_fewer_than_two_anchors_is_refused(self, cap):
        """Both ends are always anchors, so one is not a possible answer — and the widening
        arithmetic divides by `max_anchors - 1`. A test asking for one found the crash."""
        with pytest.raises(ValueError, match="max_anchors"):
            anchors(straight(10_000), max_anchors=cap)

    def test_capping_widens_the_spacing_rather_than_clustering_at_the_start(self):
        """The mean gap cannot see this, which is the point.

        Honouring the requested spacing until the cap runs out puts every anchor in the
        first 19 km of a 1,000 km route and then jumps to the end. The count is right, the
        end is present, the *mean* gap is right — and 98% of the trip was never searched.
        Only the largest gap exposes it.
        """
        route = straight(1_000_000)
        placed = anchors(route, spacing_m=1_000, max_anchors=20)
        gaps = [haversine_m(a, b) for a, b in pairwise(placed)]
        assert max(gaps) < 1_000_000 / len(placed) * 1.5
