"""Assembling the numbers the judge is handed.

The rule this stage exists to honour: measure what is measurable, ask the model only what is
not. Everything here is arithmetic on geometry already held — no provider, no model — so it
is deterministic, cheap and testable, and a model cannot be confidently wrong about it.

The local part matters. "How twisty is the route" is a trip statistic; "what is the road like
*where this campsite is*" is what decides whether stopping there is pleasant, and they can be
very different on a route that is half motorway and half mountain pass.
"""

from math import pi

import pytest

from motorooter.planning.discovery.evidence import assemble, route_window
from motorooter.planning.discovery.models import Candidate, ResolvedCandidate
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg, Surface, SurfaceSpan
from motorooter.trips.models import PoiCategory

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180


def north(metres: float, *, east: float = 0.0) -> Coordinate:
    return Coordinate(lat=metres / M_PER_DEGREE_LAT, lon=east / M_PER_DEGREE_LAT)


def leg(*points: Coordinate, spans: tuple[SurfaceSpan, ...] = ()) -> RouteLeg:
    return RouteLeg(
        geometry=points,
        distance_m=1000.0,
        duration_s=100.0,
        surface_spans=spans,
        provider="fake",
        intent=LegIntent.UNPAVED,
    )


def resolved(
    lat_m: float, *, category: PoiCategory = PoiCategory.WILD_CAMP, distance: float = 100.0
) -> ResolvedCandidate:
    return ResolvedCandidate(
        candidate=Candidate(
            name="A place",
            category=category,
            found_near=north(lat_m),
            source="brave",
        ),
        place_id="ChIJ_x",
        coordinate=north(lat_m, east=distance),
        distance_off_route_m=distance,
    )


STRAIGHT = leg(*(north(index * 100.0) for index in range(101)))


class TestTheWindowAroundAPoint:
    def test_it_selects_geometry_near_the_point(self):
        window = route_window(STRAIGHT, north(5000.0), radius_m=500.0)
        assert all(abs(point.lat * M_PER_DEGREE_LAT - 5000.0) <= 600.0 for point in window)

    def test_a_wider_radius_selects_more(self):
        assert len(route_window(STRAIGHT, north(5000.0), radius_m=2000.0)) > len(
            route_window(STRAIGHT, north(5000.0), radius_m=500.0)
        )

    def test_a_point_at_the_start_still_gets_a_window(self):
        """Half a window is a window; a POI at the trailhead is not a special case."""
        assert len(route_window(STRAIGHT, north(0.0), radius_m=500.0)) > 1

    def test_a_point_at_the_end_still_gets_a_window(self):
        assert len(route_window(STRAIGHT, north(10_000.0), radius_m=500.0)) > 1

    def test_a_point_far_from_the_route_gets_the_nearest_part(self):
        """Not empty: the road nearest a distant place is still the road you would ride."""
        assert len(route_window(STRAIGHT, north(5000.0, east=50_000.0), radius_m=500.0)) > 1

    def test_even_a_zero_radius_yields_a_segment(self):
        """Twistiness needs two points to mean anything, so the window never collapses to
        one. A `RouteLeg` cannot have fewer than two points, so there is no emptier case."""
        assert len(route_window(leg(north(0), north(1)), north(0), radius_m=0.0)) >= 2


class TestWhatTheJudgeIsHanded:
    def test_it_reports_the_distance_off_route(self):
        found = assemble(resolved(5000.0, distance=800.0), STRAIGHT)
        assert found.distance_off_route_m == 800.0

    def test_it_reports_local_twistiness_not_the_whole_route(self):
        """A campsite on the mountain half of a route should not inherit the motorway half."""
        twisty_end = leg(
            *(north(index * 100.0) for index in range(50)),
            *(north(5000.0 + index * 100.0, east=(index % 2) * 400.0) for index in range(50)),
        )
        near_straight = assemble(resolved(1000.0), twisty_end).twistiness_deg_per_km
        near_twisty = assemble(resolved(8000.0), twisty_end).twistiness_deg_per_km
        assert near_straight is not None
        assert near_twisty is not None
        assert near_twisty > near_straight

    def test_it_reports_the_local_surface_mix(self):
        """The first half of the route is dirt. A place in the middle of that half should
        read as mostly dirt — not entirely, since a 5 km window overruns the 5 km span."""
        surfaced = leg(
            *(north(index * 100.0) for index in range(101)),
            spans=(SurfaceSpan(start_index=0, end_index=50, surface=Surface.UNPAVED),),
        )
        assert assemble(resolved(1000.0), surfaced).unpaved_fraction == pytest.approx(
            0.83, abs=0.02
        )

    def test_a_tighter_window_reads_purely_local(self):
        surfaced = leg(
            *(north(index * 100.0) for index in range(101)),
            spans=(SurfaceSpan(start_index=0, end_index=50, surface=Surface.UNPAVED),),
        )
        found = assemble(resolved(2000.0), surfaced, window_m=1000.0)
        assert found.unpaved_fraction == pytest.approx(1.0)

    def test_the_far_end_of_the_same_route_reports_differently(self):
        """The whole point of a local window: one number for the route would give both
        places the same answer, and they are on quite different roads."""
        surfaced = leg(
            *(north(index * 100.0) for index in range(101)),
            spans=(SurfaceSpan(start_index=0, end_index=50, surface=Surface.UNPAVED),),
        )
        near_dirt = assemble(resolved(1000.0), surfaced).unpaved_fraction
        far_end = assemble(resolved(9000.0), surfaced).unpaved_fraction
        assert near_dirt is not None
        assert far_end is not None
        assert far_end < near_dirt / 3

    def test_unmeasured_surface_is_reported_as_unknown_not_as_paved(self):
        """The distinction the trip metrics already make; the scorer needs it too."""
        found = assemble(resolved(5000.0), STRAIGHT)
        assert found.unknown_surface_fraction == pytest.approx(1.0)
        assert found.unpaved_fraction == pytest.approx(0.0)


class TestRemoteness:
    def test_it_measures_the_distance_to_the_nearest_fuel(self):
        camp = resolved(5000.0)
        fuel = resolved(6000.0, category=PoiCategory.FUEL)
        found = assemble(camp, STRAIGHT, others=[camp, fuel])
        assert found.distance_to_fuel_m == pytest.approx(1000.0, rel=0.1)

    def test_the_nearest_of_several_wins(self):
        camp = resolved(5000.0)
        found = assemble(
            camp,
            STRAIGHT,
            others=[
                camp,
                resolved(9000.0, category=PoiCategory.FUEL),
                resolved(5500.0, category=PoiCategory.FUEL),
            ],
        )
        assert found.distance_to_fuel_m == pytest.approx(500.0, rel=0.1)

    def test_no_fuel_anywhere_reports_absent_not_zero(self):
        """Zero would read as "fuel right here", which is the opposite of the truth."""
        camp = resolved(5000.0)
        assert assemble(camp, STRAIGHT, others=[camp]).distance_to_fuel_m is None

    def test_a_fuel_stop_does_not_measure_itself(self):
        fuel = resolved(5000.0, category=PoiCategory.FUEL)
        assert assemble(fuel, STRAIGHT, others=[fuel]).distance_to_fuel_m is None


class TestItNeverInventsANumber:
    def test_an_unroutable_context_still_produces_evidence(self):
        """A missing route means missing signals, not a crash and not a zero."""
        found = assemble(resolved(5000.0), leg(north(0), north(1)))
        assert found.distance_off_route_m == 100.0

    def test_absent_signals_are_none_rather_than_zero(self):
        found = assemble(
            ResolvedCandidate(
                candidate=Candidate(
                    name="x",
                    category=PoiCategory.FOOD,
                    found_near=north(0),
                    source="brave",
                ),
                place_id="ChIJ_y",
                coordinate=north(0),
            ),
            STRAIGHT,
        )
        assert found.distance_off_route_m is None
