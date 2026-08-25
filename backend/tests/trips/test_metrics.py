"""Derived trip figures, and why they cross the wire.

These were plain properties, so they did not serialize and a frontend holding a `Trip` had to
recompute them. That is precisely the drift the generated contract exists to prevent, and it
would have landed on surface reporting first: recomputing "unpaved fraction" client-side is
how "unknown counts as paved" quietly comes back.

Three surface states, not two. A leg with no surface data is not a paved leg. Reporting only
paved-versus-unpaved forces the unknown share into one of them, and it always ends up in
whichever the UI treats as the default.
"""

from math import pi

import pytest

from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg, Surface, SurfaceSpan
from motorooter.speeds import DEFAULT_RIDING_SPEEDS
from motorooter.trips.models import Trip, TripLeg, TripSummary, Waypoint
from tests.trips.store_contract import T0

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180
"""Exact, so a nominal "80 km leg" measures 80 km and durations come out round."""


def at(metres_north: float) -> Coordinate:
    return Coordinate(lat=metres_north / M_PER_DEGREE_LAT, lon=0.0)


def routed(*metres: float, spans: tuple[SurfaceSpan, ...] = ()) -> RouteLeg:
    geometry = tuple(at(m) for m in metres)
    return RouteLeg(
        geometry=geometry,
        distance_m=metres[-1] - metres[0],
        duration_s=99_999.0,  # Deliberately absurd: nothing may read the provider's figure.
        surface_spans=spans,
        provider="fake",
        intent=LegIntent.UNPAVED,
    )


def span(start: int, end: int, surface: Surface) -> SurfaceSpan:
    return SurfaceSpan(start_index=start, end_index=end, surface=surface)


def trip_of(*legs: RouteLeg) -> Trip:
    return Trip(
        slug="oregon-backcountry",
        name="Oregon Backcountry",
        created_at=T0,
        edited_at=T0,
        waypoints=tuple(Waypoint(coordinate=at(index * 1000.0)) for index in range(len(legs) + 1)),
        legs=tuple(
            TripLeg(
                intent=LegIntent.UNPAVED,
                start_waypoint_index=index,
                end_waypoint_index=index + 1,
                routed=leg,
            )
            for index, leg in enumerate(legs)
        ),
    )


class TestThreeSurfaceStates:
    def test_a_fully_paved_trip_reports_no_dirt_and_no_unknown(self):
        trip = trip_of(routed(0, 1000, spans=(span(0, 1, Surface.PAVED),)))
        assert trip.total_unpaved_fraction == pytest.approx(0.0)
        assert trip.total_unknown_fraction == pytest.approx(0.0)

    def test_a_fully_unpaved_trip_reports_all_dirt(self):
        trip = trip_of(routed(0, 1000, spans=(span(0, 1, Surface.UNPAVED),)))
        assert trip.total_unpaved_fraction == pytest.approx(1.0)

    def test_geometry_with_no_spans_at_all_is_unknown_not_paved(self):
        """The default must not be tarmac. Nobody measured this road."""
        trip = trip_of(routed(0, 1000))
        assert trip.total_unknown_fraction == pytest.approx(1.0)
        assert trip.total_unpaved_fraction == pytest.approx(0.0)

    def test_an_explicit_unknown_span_counts_as_unknown(self):
        trip = trip_of(routed(0, 1000, spans=(span(0, 1, Surface.UNKNOWN),)))
        assert trip.total_unknown_fraction == pytest.approx(1.0)

    def test_geometry_no_span_covers_counts_as_unknown(self):
        """A span over half the leg leaves the other half unmeasured, not paved."""
        trip = trip_of(routed(0, 500, 1000, spans=(span(0, 1, Surface.PAVED),)))
        assert trip.total_unknown_fraction == pytest.approx(0.5, rel=1e-6)

    def test_the_three_shares_sum_to_one(self):
        trip = trip_of(
            routed(0, 250, 500, 750, spans=(span(0, 1, Surface.PAVED), span(1, 2, Surface.UNPAVED)))
        )
        total = (
            trip.total_paved_fraction + trip.total_unpaved_fraction + trip.total_unknown_fraction
        )
        assert total == pytest.approx(1.0, rel=1e-9)

    def test_an_unrouted_trip_reports_zero_rather_than_dividing_by_zero(self):
        empty = Trip(slug="a-trip", name="A Trip", created_at=T0, edited_at=T0)
        assert empty.total_unpaved_fraction == 0.0
        assert empty.total_unknown_fraction == 0.0


class TestEstimatedDuration:
    def test_it_ignores_the_providers_figure(self):
        """`duration_s` on the leg is bicycle time from cycling-mountain. Never read it."""
        trip = trip_of(routed(0, 80_000, spans=(span(0, 1, Surface.PAVED),)))
        assert trip.estimated_duration_s != pytest.approx(99_999.0)

    def test_paved_distance_is_estimated_at_the_paved_speed(self):
        trip = trip_of(routed(0, 80_000, spans=(span(0, 1, Surface.PAVED),)))
        expected = DEFAULT_RIDING_SPEEDS.seconds_for(80_000.0, Surface.PAVED)
        assert trip.estimated_duration_s == pytest.approx(expected, rel=1e-6)

    def test_the_same_distance_on_dirt_takes_longer(self):
        paved = trip_of(routed(0, 50_000, spans=(span(0, 1, Surface.PAVED),)))
        dirt = trip_of(routed(0, 50_000, spans=(span(0, 1, Surface.UNPAVED),)))
        assert dirt.estimated_duration_s > paved.estimated_duration_s

    def test_a_mixed_trip_weights_each_surface_separately(self):
        trip = trip_of(
            routed(0, 500, 1000, spans=(span(0, 1, Surface.PAVED), span(1, 2, Surface.UNPAVED)))
        )
        expected = DEFAULT_RIDING_SPEEDS.seconds_for(
            500.0, Surface.PAVED
        ) + DEFAULT_RIDING_SPEEDS.seconds_for(500.0, Surface.UNPAVED)
        assert trip.estimated_duration_s == pytest.approx(expected, rel=1e-6)

    def test_it_sums_across_legs(self):
        one = trip_of(routed(0, 1000, spans=(span(0, 1, Surface.PAVED),)))
        two = trip_of(
            routed(0, 1000, spans=(span(0, 1, Surface.PAVED),)),
            routed(1000, 2000, spans=(span(0, 1, Surface.PAVED),)),
        )
        assert two.estimated_duration_s == pytest.approx(one.estimated_duration_s * 2, rel=1e-6)

    def test_an_unrouted_trip_takes_no_time(self):
        assert (
            Trip(slug="a-trip", name="A Trip", created_at=T0, edited_at=T0).estimated_duration_s
            == 0.0
        )


class TestTheyCrossTheWire:
    """A property does not serialize, so the client recomputes it and the two drift."""

    @pytest.fixture
    def document(self):
        return trip_of(
            routed(0, 500, 1000, spans=(span(0, 1, Surface.PAVED), span(1, 2, Surface.UNPAVED)))
        ).model_dump()

    @pytest.mark.parametrize(
        "field",
        [
            "total_distance_m",
            "total_paved_fraction",
            "total_unpaved_fraction",
            "total_unknown_fraction",
            "estimated_duration_s",
        ],
    )
    def test_the_derived_figure_is_serialized(self, document, field):
        assert field in document

    def test_the_serialized_value_matches_the_computed_one(self, document):
        trip = trip_of(
            routed(0, 500, 1000, spans=(span(0, 1, Surface.PAVED), span(1, 2, Surface.UNPAVED)))
        )
        assert document["estimated_duration_s"] == pytest.approx(trip.estimated_duration_s)

    def test_a_trip_still_round_trips_through_json(self):
        """Computed fields are emitted but not accepted back; reloading must still work."""
        trip = trip_of(routed(0, 1000, spans=(span(0, 1, Surface.UNPAVED),)))
        assert Trip.model_validate_json(trip.model_dump_json()) == trip

    def test_a_stale_computed_value_in_a_stored_document_is_ignored(self):
        """The bucket holds derived numbers. They must never be read back as truth."""
        trip = trip_of(routed(0, 1000, spans=(span(0, 1, Surface.UNPAVED),)))
        tampered = trip.model_dump_json().replace(
            f'"estimated_duration_s":{trip.estimated_duration_s}', '"estimated_duration_s":1.0'
        )
        assert Trip.model_validate_json(tampered).estimated_duration_s == trip.estimated_duration_s


class TestTheSummaryCarriesTheSameNumbers:
    """Nothing derived twice — the index and the trip must agree by construction."""

    @pytest.fixture
    def trip(self):
        return trip_of(
            routed(0, 500, 1000, spans=(span(0, 1, Surface.PAVED), span(1, 2, Surface.UNPAVED)))
        )

    @pytest.mark.parametrize(
        "field",
        [
            "total_distance_m",
            "total_paved_fraction",
            "total_unpaved_fraction",
            "total_unknown_fraction",
            "estimated_duration_s",
        ],
    )
    def test_the_summary_reports_what_the_trip_reports(self, trip, field):
        summary = TripSummary.from_trip(trip)
        assert getattr(summary, field) == pytest.approx(getattr(trip, field))

    def test_the_summary_still_ships_no_geometry(self, trip):
        assert "legs" not in TripSummary.from_trip(trip).model_dump()
