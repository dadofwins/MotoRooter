"""Which of the places discovery found are worth changing the route for.

This is an autonomous edit to something the rider built, so the bar is not "is this place
good" but "is this place good enough that taking the decision away from them is a favour".
Three bounds, each doing a different job, all of them arithmetic:

- a **floor**, so nothing the judge itself hedged about gets added,
- a **detour budget**, which is the bound in the units the harm actually occurs in,
- a **count**, so a route does not become a bus tour.

The count is normally what binds. That is deliberate — see the module docstring.
"""

from math import cos, pi, radians

import pytest

from motorooter.planning.discovery.selection import (
    DETOUR_BUDGET_FRACTION,
    HOURS_PER_ADDITION,
    MIN_SCORE,
    default_limit,
    worth_routing_through,
)
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    RouteLeg,
    Surface,
    SurfaceSpan,
)
from motorooter.trips.models import Poi, PoiCategory, PoiSource

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180


def north(metres: float, *, east: float = 0.0) -> Coordinate:
    lat = metres / M_PER_DEGREE_LAT
    lon = east / (M_PER_DEGREE_LAT * cos(radians(lat)))
    return Coordinate(lat=lat, lon=lon)


def leg(length_m: float = 400_000.0, *, surface: Surface = Surface.PAVED) -> RouteLeg:
    """A leg whose geometry really is `length_m` long, running due north from the origin.

    400 km of tarmac is five riding hours, which is two additions under the default rate —
    enough headroom that a test about the floor is not silently a test about the count.
    """
    points = tuple(north(length_m * index / 100) for index in range(101))
    return RouteLeg(
        geometry=points,
        distance_m=length_m,
        duration_s=0.0,
        surface_spans=(
            SurfaceSpan(
                surface=surface, distance_m=length_m, start_index=0, end_index=len(points) - 1
            ),
        ),
        provider="fake",
        intent=LegIntent.TWISTY_PAVED,
    )


def poi(
    name: str,
    score: float | None,
    *,
    along: float = 0.5,
    off_route_m: float = 100.0,
    on_route: bool = False,
    length_m: float = 400_000.0,
) -> Poi:
    return Poi(
        id=f"poi-{name}",
        name=name,
        category=PoiCategory.VIEWPOINT,
        coordinate=north(length_m * along, east=off_route_m),
        source=PoiSource.PLACES,
        place_id=f"ChIJ-{name}",
        score=score,
        on_route=on_route,
    )


def names(chosen):
    return [place.name for place in chosen]


class TestTheFloor:
    def test_a_place_the_judge_recommended_is_taken(self):
        assert names(worth_routing_through([poi("lookout", 0.9)], leg=leg())) == ["lookout"]

    def test_a_place_the_judge_hedged_about_is_left_for_the_rider(self):
        assert worth_routing_through([poi("meh", MIN_SCORE - 0.01)], leg=leg()) == ()

    def test_the_floor_itself_is_included_not_excluded(self):
        assert names(worth_routing_through([poi("edge", MIN_SCORE)], leg=leg())) == ["edge"]

    def test_a_place_nobody_judged_is_never_added_automatically(self):
        """A pin the rider dropped has no score. Silence is not consent to reroute."""
        assert worth_routing_through([poi("hand-dropped", None)], leg=leg()) == ()

    def test_the_floor_sits_where_the_judge_stops_recommending(self):
        """Read off real reasons: unhedged at 0.75, qualified at 0.60, negative at 0.45.

        Tuning this to produce a pleasing count is the failure mode; it is a rung on a
        ladder someone else can inspect.
        """
        assert 0.6 < MIN_SCORE <= 0.75


class TestTheCount:
    def test_it_takes_the_best_first(self):
        found = [poi("ok", 0.72, along=0.2), poi("great", 0.95, along=0.8)]
        assert names(worth_routing_through(found, leg=leg(), limit=1)) == ["great"]

    def test_it_stops_at_the_limit_it_was_given(self):
        found = [poi(f"p{index}", 0.9, along=index / 10) for index in range(9)]
        assert len(worth_routing_through(found, leg=leg(), limit=3)) == 3

    def test_asking_for_none_adds_none(self):
        assert worth_routing_through([poi("great", 0.95)], leg=leg(), limit=0) == ()

    def test_the_default_is_one_per_two_hours_of_riding(self):
        assert HOURS_PER_ADDITION == 2.0
        assert default_limit(leg(400_000.0)) == 2  # 400 km of tarmac is five hours

    def test_a_short_ride_still_gets_one(self):
        """Nought would make the feature silently do nothing on a two-hour ride."""
        assert default_limit(leg(40_000.0)) == 1

    def test_dirt_earns_more_stops_than_tarmac_over_the_same_distance(self):
        """The rate is per hour, and the same distance of dirt is a much longer day."""
        assert default_limit(leg(400_000.0, surface=Surface.UNPAVED)) > default_limit(
            leg(400_000.0, surface=Surface.PAVED)
        )

    def test_the_default_applies_when_no_limit_is_asked_for(self):
        found = [poi(f"p{index}", 0.9, along=index / 10) for index in range(9)]
        assert len(worth_routing_through(found, leg=leg())) == default_limit(leg())


class TestTheDetourBudget:
    def test_a_place_beside_the_road_costs_almost_nothing(self):
        found = [poi("roadside", 0.9, off_route_m=50.0)]
        assert names(worth_routing_through(found, leg=leg())) == ["roadside"]

    def test_a_place_that_would_reshape_the_trip_is_refused(self):
        """One 40 km detour off a 400 km leg is 80 km added — a fifth of the ride again."""
        found = [poi("far", 0.95, off_route_m=40_000.0)]
        assert worth_routing_through(found, leg=leg(400_000.0)) == ()

    def test_the_budget_is_spent_across_all_the_additions_not_each_one(self):
        """Two detours that each fit can still be one too many together."""
        budget = DETOUR_BUDGET_FRACTION * 400_000.0
        each = budget * 0.4  # two fit the budget, three do not
        found = [
            poi(f"p{index}", 0.9 - index / 100, along=0.2 + index / 5, off_route_m=each / 2)
            for index in range(3)
        ]
        assert len(worth_routing_through(found, leg=leg(400_000.0), limit=3)) == 2

    def test_it_keeps_looking_after_one_place_proves_too_expensive(self):
        """A cheap good place behind an expensive one must not be lost to it."""
        found = [poi("far", 0.95, off_route_m=40_000.0), poi("near", 0.8, off_route_m=100.0)]
        assert names(worth_routing_through(found, leg=leg(400_000.0), limit=2)) == ["near"]

    def test_the_cost_over_states_an_out_and_back(self):
        """Twice the off-route distance. Over-stating fails in the safe direction."""
        budget = DETOUR_BUDGET_FRACTION * 400_000.0
        just_over = budget / 2 * 1.1
        assert worth_routing_through([poi("far", 0.9, off_route_m=just_over)], leg=leg()) == ()

    def test_a_leg_with_no_length_admits_nothing_rather_than_everything(self):
        """Zero budget, not unlimited budget — the failure that adds every place at once."""
        empty = RouteLeg(
            geometry=(north(0), north(0)),
            distance_m=0.0,
            duration_s=0.0,
            surface_spans=(),
            provider="fake",
            intent=LegIntent.TWISTY_PAVED,
        )
        assert worth_routing_through([poi("anything", 0.99)], leg=empty) == ()


class TestWhatItLeavesAlone:
    def test_a_place_already_on_the_route_is_not_added_twice(self):
        found = [poi("already", 0.95, on_route=True), poi("new", 0.8)]
        assert names(worth_routing_through(found, leg=leg(), limit=2)) == ["new"]

    def test_nothing_found_is_nothing_added(self):
        assert worth_routing_through([], leg=leg()) == ()

    def test_it_returns_them_best_first(self):
        found = [poi("second", 0.8, along=0.2), poi("first", 0.95, along=0.8)]
        assert names(worth_routing_through(found, leg=leg(), limit=2)) == ["first", "second"]

    @pytest.mark.parametrize("limit", [-1, -10])
    def test_a_nonsense_limit_adds_nothing_rather_than_everything(self, limit):
        assert worth_routing_through([poi("great", 0.95)], leg=leg(), limit=limit) == ()
