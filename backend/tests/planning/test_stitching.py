"""Joining routed legs into one continuous geometry.

Leg boundaries are where the bugs live, so nearly everything here is a boundary test. Two
failure modes drive the design:

- **Index drift.** Surface spans are indices into a leg's own geometry. Stitching shifts
  every one of them, and by a different amount depending on whether the boundary vertex
  was deduplicated. Get it wrong and the dirt statistic silently reports the wrong
  segments — silently, because the geometry still renders fine.
- **Endpoint mismatch.** Two engines snapping the same waypoint to different nodes leave
  a hole. Papering over it hides a routing problem; ignoring it renders a teleport.
"""

from math import pi

import pytest

from motorooter.planning.stitching import (
    COINCIDENT_TOLERANCE_M,
    GAP_REPORT_THRESHOLD_M,
    stitch,
)
from motorooter.routing.geo import EARTH_RADIUS_M, haversine_m, path_length_m
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg, Surface, SurfaceSpan

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180
"""~111.2 km. Lets a test say "1000 metres north" and mean it."""


def at(metres_north: float) -> Coordinate:
    """A point `metres_north` of the origin, on the prime meridian."""
    return Coordinate(lat=metres_north / M_PER_DEGREE_LAT, lon=0.0)


def leg(
    *metres: float,
    spans: tuple[SurfaceSpan, ...] = (),
    provider: str = "fake",
    intent: LegIntent = LegIntent.UNPAVED,
    duration_s: float = 100.0,
) -> RouteLeg:
    """A leg whose vertices sit at the given distances north of the origin."""
    geometry = tuple(at(m) for m in metres)
    return RouteLeg(
        geometry=geometry,
        distance_m=path_length_m(geometry),
        duration_s=duration_s,
        surface_spans=spans,
        provider=provider,
        intent=intent,
    )


def unpaved(start: int, end: int) -> SurfaceSpan:
    return SurfaceSpan(start_index=start, end_index=end, surface=Surface.UNPAVED)


def paved(start: int, end: int) -> SurfaceSpan:
    return SurfaceSpan(start_index=start, end_index=end, surface=Surface.PAVED)


class TestDegenerateInput:
    def test_no_legs_gives_an_empty_route(self):
        """An unrouted trip is a legitimate state, not an error."""
        route = stitch([])
        assert route.geometry == ()
        assert route.gaps == ()
        assert route.distance_m == 0.0

    def test_a_single_leg_passes_through_unchanged(self):
        original = leg(0, 500, 1000, spans=(unpaved(0, 2),))
        route = stitch([original])
        assert route.geometry == original.geometry
        assert route.surface_spans == original.surface_spans
        assert route.gaps == ()


class TestJoiningAtACoincidentBoundary:
    """The common case: both engines snapped the shared waypoint to the same node."""

    def test_the_shared_vertex_appears_once(self):
        route = stitch([leg(0, 500, 1000), leg(1000, 1500, 2000)])
        assert len(route.geometry) == 5

    def test_the_joined_geometry_runs_end_to_end(self):
        route = stitch([leg(0, 500, 1000), leg(1000, 1500, 2000)])
        assert route.geometry[0] == at(0)
        assert route.geometry[-1] == at(2000)

    def test_no_two_consecutive_vertices_are_identical(self):
        """A zero-length segment is a rendering artefact and skews nothing but confuses."""
        route = stitch([leg(0, 500, 1000), leg(1000, 1500, 2000)])
        assert all(a != b for a, b in zip(route.geometry, route.geometry[1:], strict=False))

    def test_geometry_length_matches_the_two_legs_end_to_end(self):
        route = stitch([leg(0, 500, 1000), leg(1000, 1500, 2000)])
        assert route.geometry_length_m == pytest.approx(2000.0, rel=1e-6)

    def test_a_sub_tolerance_mismatch_still_counts_as_coincident(self):
        """Float jitter between engines must not manufacture a gap."""
        route = stitch([leg(0, 1000), leg(1000 + COINCIDENT_TOLERANCE_M / 2, 2000)])
        assert route.gaps == ()
        assert len(route.geometry) == 3


class TestJoiningAcrossAGap:
    """Two engines snapped the shared waypoint to different nodes."""

    def test_both_endpoints_are_kept(self):
        route = stitch([leg(0, 1000), leg(1200, 2000)])
        assert len(route.geometry) == 4

    def test_the_gap_is_reported_with_its_distance(self):
        route = stitch([leg(0, 1000), leg(1200, 2000)])
        assert len(route.gaps) == 1
        assert route.gaps[0].distance_m == pytest.approx(200.0, rel=1e-6)

    def test_the_gap_names_the_leg_it_follows(self):
        route = stitch([leg(0, 1000), leg(1200, 2000), leg(2000, 3000)])
        assert [gap.after_leg_index for gap in route.gaps] == [0]

    def test_the_gap_carries_both_endpoints(self):
        route = stitch([leg(0, 1000), leg(1200, 2000)])
        gap = route.gaps[0]
        assert haversine_m(gap.end, at(1000)) < 0.01
        assert haversine_m(gap.start, at(1200)) < 0.01

    def test_a_mismatch_below_the_report_threshold_is_not_reported(self):
        """Engines snap to different nodes on the same road constantly; that is not a bug."""
        route = stitch([leg(0, 1000), leg(1000 + GAP_REPORT_THRESHOLD_M / 2, 2000)])
        assert route.gaps == ()

    def test_a_mismatch_below_the_report_threshold_still_keeps_both_vertices(self):
        """Not worth warning about, but still not the same point — do not fabricate a merge."""
        route = stitch([leg(0, 1000), leg(1000 + GAP_REPORT_THRESHOLD_M / 2, 2000)])
        assert len(route.geometry) == 4

    def test_the_route_reports_itself_discontinuous(self):
        assert stitch([leg(0, 1000), leg(1200, 2000)]).is_continuous is False

    def test_a_clean_route_reports_itself_continuous(self):
        assert stitch([leg(0, 1000), leg(1000, 2000)]).is_continuous is True

    def test_different_engines_meeting_imperfectly_is_the_headline_case(self):
        """Exactly the scenario the architecture exists to support, and its known hazard."""
        route = stitch(
            [
                leg(0, 1000, provider="google", intent=LegIntent.HIGHWAY_CONNECTOR),
                leg(1150, 2000, provider="ors", intent=LegIntent.UNPAVED),
            ]
        )
        assert [leg_.provider for leg_ in route.legs] == ["google", "ors"]
        assert len(route.gaps) == 1
        assert route.geometry[1] != route.geometry[2]


class TestSurfaceSpanReindexing:
    """Spans are indices into a leg's own geometry; stitching has to move every one."""

    def test_spans_from_the_first_leg_keep_their_indices(self):
        route = stitch([leg(0, 500, 1000, spans=(unpaved(0, 2),)), leg(1000, 2000)])
        assert route.surface_spans[0].start_index == 0
        assert route.surface_spans[0].end_index == 2

    def test_spans_from_a_later_leg_shift_by_the_preceding_geometry(self):
        """Leg 2 starts at the shared vertex, index 2 — not index 3."""
        route = stitch([leg(0, 500, 1000), leg(1000, 1500, 2000, spans=(unpaved(0, 2),))])
        shifted = route.surface_spans[0]
        assert shifted.start_index == 2
        assert shifted.end_index == 4

    def test_spans_shift_further_when_a_gap_kept_both_vertices(self):
        """One extra vertex survives the join, so every later index moves one further."""
        route = stitch([leg(0, 500, 1000), leg(1200, 1500, 2000, spans=(unpaved(0, 2),))])
        shifted = route.surface_spans[0]
        assert shifted.start_index == 3
        assert shifted.end_index == 5

    def test_offsets_compound_across_three_legs(self):
        route = stitch(
            [
                leg(0, 500, 1000),
                leg(1000, 1500, 2000),
                leg(2000, 2500, 3000, spans=(unpaved(0, 2),)),
            ]
        )
        # 3 vertices, then 2 more, then this leg starts at the shared vertex: index 4.
        assert route.surface_spans[0].start_index == 4
        assert route.surface_spans[0].end_index == 6

    def test_every_span_stays_inside_the_joined_geometry(self):
        route = stitch(
            [
                leg(0, 500, 1000, spans=(unpaved(0, 1), paved(1, 2))),
                leg(1200, 1500, 2000, spans=(paved(0, 1), unpaved(1, 2))),
            ]
        )
        limit = len(route.geometry) - 1
        assert all(span.end_index <= limit for span in route.surface_spans)

    def test_spans_from_every_leg_survive(self):
        route = stitch(
            [
                leg(0, 500, 1000, spans=(unpaved(0, 1), paved(1, 2))),
                leg(1000, 1500, 2000, spans=(paved(0, 1), unpaved(1, 2))),
            ]
        )
        assert len(route.surface_spans) == 4

    def test_a_span_covering_the_shared_vertex_still_measures_the_same_ground(self):
        """The reindexed span must cover the same metres it did inside its own leg."""
        second = leg(1000, 1500, 2000, spans=(unpaved(0, 2),))
        route = stitch([leg(0, 500, 1000), second])
        span = route.surface_spans[0]
        covered = path_length_m(route.geometry[span.start_index : span.end_index + 1])
        assert covered == pytest.approx(second.unpaved_distance_m, rel=1e-6)


class TestSurfaceAccounting:
    """The dirt statistic is what the trip is judged on; stitching must not move it."""

    def test_unpaved_distance_is_preserved_across_a_clean_join(self):
        legs = [
            leg(0, 500, 1000, spans=(unpaved(0, 2),)),
            leg(1000, 1500, 2000, spans=(unpaved(0, 2),)),
        ]
        route = stitch(legs)
        assert route.unpaved_distance_m == pytest.approx(
            sum(leg_.unpaved_distance_m for leg_ in legs), rel=1e-6
        )

    def test_a_bridged_gap_is_not_counted_as_dirt(self):
        """The bridge is fabricated geometry. Letting it inflate the dirt figure is a lie."""
        route = stitch(
            [
                leg(0, 1000, spans=(unpaved(0, 1),)),
                leg(1200, 2000, spans=(unpaved(0, 1),)),
            ]
        )
        assert route.unpaved_distance_m == pytest.approx(1800.0, rel=1e-6)

    def test_the_bridge_still_counts_toward_the_geometry_length(self):
        """It is distance the rider covers, so it belongs in the denominator."""
        route = stitch([leg(0, 1000), leg(1200, 2000)])
        assert route.geometry_length_m == pytest.approx(2000.0, rel=1e-6)

    def test_unpaved_fraction_is_weighted_by_distance_not_averaged_per_leg(self):
        """A short dirt connector beside a long highway must not read as half the trip."""
        route = stitch(
            [
                leg(0, 100, spans=(unpaved(0, 1),)),
                leg(100, 10_000, spans=(paved(0, 1),)),
            ]
        )
        assert route.unpaved_fraction == pytest.approx(0.01, rel=1e-6)

    def test_no_surface_data_reads_as_no_dirt_not_as_unknown_dirt(self):
        assert stitch([leg(0, 1000), leg(1000, 2000)]).unpaved_fraction == 0.0


class TestTotals:
    def test_distance_sums_the_providers_reported_figures(self):
        """Matches `Trip.total_distance_m`, so the two never disagree on the same trip."""
        legs = [leg(0, 1000), leg(1200, 2000)]
        route = stitch(legs)
        assert route.distance_m == pytest.approx(sum(leg_.distance_m for leg_ in legs))

    def test_bridged_distance_is_reported_separately_rather_than_folded_in(self):
        """Fabricated metres stay visible instead of quietly padding the trip length."""
        route = stitch([leg(0, 1000), leg(1200, 2000)])
        assert route.bridged_distance_m == pytest.approx(200.0, rel=1e-6)
        assert route.distance_m == pytest.approx(1800.0, rel=1e-6)

    def test_duration_sums_the_legs(self):
        route = stitch([leg(0, 1000, duration_s=60.0), leg(1000, 2000, duration_s=90.0)])
        assert route.duration_s == pytest.approx(150.0)

    def test_legs_are_preserved_in_order(self):
        legs = [leg(0, 1000), leg(1000, 2000), leg(2000, 3000)]
        assert stitch(legs).legs == tuple(legs)


class TestTunableThresholds:
    def test_the_coincidence_tolerance_is_configurable(self):
        route = stitch(
            [leg(0, 1000), leg(1050, 2000)],
            coincident_tolerance_m=100.0,
            gap_threshold_m=200.0,
        )
        assert len(route.geometry) == 3
        assert route.gaps == ()

    def test_the_report_threshold_is_configurable(self):
        route = stitch([leg(0, 1000), leg(1050, 2000)], gap_threshold_m=10.0)
        assert len(route.gaps) == 1

    def test_a_report_threshold_below_the_coincidence_tolerance_is_rejected(self):
        """It would claim a gap between two vertices it had just merged into one."""
        with pytest.raises(ValueError, match="tolerance"):
            stitch([leg(0, 1000)], coincident_tolerance_m=50.0, gap_threshold_m=10.0)
