"""What the trip document says about itself, before any model sees it.

The measurable half of the blurb. Every figure the model is allowed to use comes from here,
so that "never state a number you were not given" is enforceable rather than hoped for: if a
number is not in these facts, the model had no way to know it.
"""

import pytest

from motorooter.blurb.facts import TripFacts, facts_for
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    RouteLeg,
    Surface,
    SurfaceSpan,
)
from motorooter.trips.models import Poi, PoiCategory, PoiSource, Trip, TripLeg, Waypoint, utc_now


def coord(lat: float, lon: float = -120.66) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


def routed(
    *,
    points: tuple[Coordinate, ...],
    distance_m: float = 10_000.0,
    spans: tuple[SurfaceSpan, ...] = (),
    intent: LegIntent = LegIntent.UNPAVED,
) -> RouteLeg:
    return RouteLeg(
        geometry=points,
        distance_m=distance_m,
        duration_s=1_800.0,
        surface_spans=spans,
        provider="fake",
        intent=intent,
    )


def trip(
    *,
    waypoints: tuple[Waypoint, ...] = (),
    legs: tuple[TripLeg, ...] = (),
    pois: tuple[Poi, ...] = (),
    default_intent: LegIntent | None = None,
) -> Trip:
    now = utc_now()
    return Trip(
        slug="leavenworth-loop",
        name="Leavenworth Loop",
        created_at=now,
        edited_at=now,
        waypoints=waypoints,
        legs=legs,
        pois=pois,
        default_intent=default_intent,
    )


def named(name: str, lat: float, lon: float = -120.66) -> Waypoint:
    return Waypoint(coordinate=coord(lat, lon), name=name)


def poi(name: str, category: PoiCategory) -> Poi:
    return Poi(
        id=name.lower().replace(" ", "-"),
        name=name,
        category=category,
        coordinate=coord(47.6),
        source=PoiSource.PLACES,
    )


class TestAnEmptyTrip:
    """The frontend will not call it for an empty trip, but the next caller might."""

    def test_it_produces_facts_rather_than_raising(self):
        assert isinstance(facts_for(trip()), TripFacts)

    def test_it_reports_nothing_rather_than_zeroes_that_read_as_measurements(self):
        facts = facts_for(trip())
        assert facts.waypoint_names == ()
        assert facts.distance_km is None
        assert facts.unpaved_share is None


class TestWaypoints:
    def test_it_lists_the_names_in_order(self):
        document = trip(waypoints=(named("Leavenworth", 47.59), named("Blewett Pass", 47.34)))
        assert facts_for(document).waypoint_names == ("Leavenworth", "Blewett Pass")

    def test_an_unnamed_waypoint_is_skipped_rather_than_called_unnamed(self):
        """A name is a search term the model may repeat; a placeholder is not."""
        document = trip(
            waypoints=(named("Leavenworth", 47.59), Waypoint(coordinate=coord(47.4)))
        )
        assert facts_for(document).waypoint_names == ("Leavenworth",)

    def test_a_loop_is_recognised_as_one(self):
        """Tim's example trip is a loop, and that is the single most characterising fact."""
        document = trip(waypoints=(named("Leavenworth", 47.59), named("Leavenworth", 47.59)))
        assert facts_for(document).is_loop is True

    def test_a_point_to_point_trip_is_not_a_loop(self):
        document = trip(waypoints=(named("Leavenworth", 47.59), named("Cashmere", 47.51)))
        assert facts_for(document).is_loop is False

    def test_one_waypoint_is_not_a_loop(self):
        assert facts_for(trip(waypoints=(named("Leavenworth", 47.59),))).is_loop is False


class TestDistanceAndSurface:
    """The three shares, computed by the domain rather than here.

    `unknown` is the remainder, not the sum of UNKNOWN spans: geometry no span covers is
    exactly as unsurveyed as geometry tagged unsurveyed. Recomputing it by hand got it wrong
    once already, in `_surface_line`.
    """

    def a_routed_trip(self) -> Trip:
        leg = routed(
            points=(coord(47.0), coord(47.1), coord(47.2)),
            spans=(SurfaceSpan(start_index=0, end_index=1, surface=Surface.UNPAVED),),
        )
        return trip(
            waypoints=(named("Leavenworth", 47.0), named("Blewett", 47.2)),
            legs=(
                TripLeg(
                    intent=LegIntent.UNPAVED,
                    start_waypoint_index=0,
                    end_waypoint_index=1,
                    routed=leg,
                ),
            ),
        )

    def test_it_reports_distance_in_kilometres(self):
        assert facts_for(self.a_routed_trip()).distance_km == pytest.approx(22.2, abs=1.0)

    def test_the_three_shares_sum_to_one(self):
        facts = facts_for(self.a_routed_trip())
        shares = (facts.unpaved_share, facts.paved_share, facts.unsurveyed_share)
        assert None not in shares
        assert sum(share for share in shares if share is not None) == pytest.approx(1.0)

    def test_unsurveyed_is_reported_separately_from_paved(self):
        """The product decision the whole surface section of CLAUDE.md exists to protect."""
        facts = facts_for(self.a_routed_trip())
        assert facts.unsurveyed_share is not None
        assert facts.unsurveyed_share > 0
        assert facts.paved_share == pytest.approx(0.0)

    def test_an_unrouted_trip_reports_no_surface_at_all(self):
        """Not zero percent dirt — nothing is known yet, and zero would read as an answer."""
        document = trip(
            waypoints=(named("Leavenworth", 47.0), named("Blewett", 47.2)),
            legs=(TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1),),
        )
        facts = facts_for(document)
        assert facts.unpaved_share is None
        assert facts.distance_km is None


class TestRidingModeAndPlaces:
    def test_it_reports_the_modes_actually_in_use(self):
        legs = (
            TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1),
            TripLeg(
                intent=LegIntent.HIGHWAY_CONNECTOR,
                start_waypoint_index=1,
                end_waypoint_index=2,
            ),
        )
        points = (named("Leavenworth", 47.59), named("Blewett", 47.34), named("Cle Elum", 47.19))
        facts = facts_for(trip(waypoints=points, legs=legs))
        assert set(facts.riding_modes) == {"Offroad", "Fast"}

    def test_it_falls_back_to_the_trip_default_when_there_are_no_legs(self):
        """A one-waypoint trip still knows what the rider asked for."""
        facts = facts_for(trip(default_intent=LegIntent.UNPAVED))
        assert facts.riding_modes == ("Offroad",)

    def test_it_counts_places_by_category(self):
        pois = (
            poi("Halfway Flat", PoiCategory.WILD_CAMP),
            poi("Eagle Creek", PoiCategory.WILD_CAMP),
            poi("South Diner", PoiCategory.FOOD),
        )
        facts = facts_for(trip(pois=pois))
        assert facts.place_counts == {"wild_camp": 2, "food": 1}

    def test_it_names_a_few_places_so_the_line_can_be_specific(self):
        pois = tuple(poi(f"Camp {index}", PoiCategory.WILD_CAMP) for index in range(10))
        facts = facts_for(trip(pois=pois))
        assert 0 < len(facts.place_names) <= 5
        assert set(facts.place_names) <= {p.name for p in pois}
