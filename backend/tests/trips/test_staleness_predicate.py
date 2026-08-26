"""Whether a leg's stored geometry still describes the leg it belongs to.

This question was answered in one place — `TripRouter._stale_leg_indices`, deciding whether a
trip is safe to export — and is now needed in a second, because rebuilding a trip's legs after
a waypoint edit should not throw away geometry that is still perfectly good. Two answers to
this question would be two answers to "is this route what the rider is looking at", so it is
one function and the exporter reads it too.

The rule it encodes, from the original: **compare the request, not the endpoints.** Engines
snap to the nearest routable node, sometimes by hundreds of metres, so no tolerance separates
snapping from a rider dragging the point.
"""

from datetime import UTC, datetime

import pytest

from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    RouteFingerprint,
    RouteLeg,
    RouteRequest,
)
from motorooter.trips.models import TripLeg, Waypoint

T0 = datetime(2026, 8, 26, tzinfo=UTC)
START = Coordinate(lat=47.0, lon=-121.0)
END = Coordinate(lat=47.5, lon=-120.5)
ELSEWHERE = Coordinate(lat=48.0, lon=-119.0)


def span(start: Coordinate = START, end: Coordinate = END) -> tuple[Waypoint, ...]:
    return (Waypoint(coordinate=start), Waypoint(coordinate=end))


def geometry(
    *,
    intent: LegIntent = LegIntent.UNPAVED,
    fingerprint_of: tuple[Coordinate, ...] | None = (START, END),
    fingerprint_intent: LegIntent | None = None,
    provider_override: str | None = None,
) -> RouteLeg:
    stamp = None
    if fingerprint_of is not None:
        stamp = RouteFingerprint.of(
            RouteRequest(waypoints=fingerprint_of, intent=fingerprint_intent or intent),
            provider_override=provider_override,
        )
    return RouteLeg(
        geometry=(START, END),
        distance_m=50_000.0,
        duration_s=3600.0,
        provider="fake",
        intent=intent,
        routed_from=stamp,
    )


def leg(routed: RouteLeg | None, **overrides: object) -> TripLeg:
    fields: dict[str, object] = {
        "intent": LegIntent.UNPAVED,
        "start_waypoint_index": 0,
        "end_waypoint_index": 1,
        "routed": routed,
    }
    return TripLeg(**{**fields, **overrides})


class TestGeometryWorthKeeping:
    def test_geometry_from_this_exact_request_is_current(self):
        assert leg(geometry()).has_current_geometry(span()) is True

    def test_no_geometry_is_not_current_geometry(self):
        """A different question from "is it stale", and the one a rebuild is asking."""
        assert leg(None).has_current_geometry(span()) is False

    def test_a_moved_waypoint_makes_it_stale(self):
        assert leg(geometry()).has_current_geometry(span(end=ELSEWHERE)) is False

    def test_a_changed_mode_makes_it_stale(self):
        """Dirt geometry under a Fast label is worse than no geometry."""
        stale = leg(geometry(intent=LegIntent.UNPAVED), intent=LegIntent.HIGHWAY_CONNECTOR)
        assert stale.has_current_geometry(span()) is False

    def test_a_changed_provider_override_makes_it_stale(self):
        pinned = leg(geometry(provider_override="ors"), provider_override="google")
        assert pinned.has_current_geometry(span()) is False

    def test_snapping_does_not_make_it_stale(self):
        """The whole reason it compares the request. Engines move the line, not the ask."""
        snapped = geometry().model_copy(
            update={"geometry": (Coordinate(lat=47.003, lon=-121.004), END)}
        )
        assert leg(snapped).has_current_geometry(span()) is True


class TestGeometryWrittenBeforeFingerprintsExisted:
    """`routed_from` is optional, and old trip documents in the bucket do not have it.

    Refusing them would turn a missing annotation into a broken trip, so they fall back to the
    weaker intent comparison — which is what the exporter has always done.
    """

    def test_a_leg_with_no_fingerprint_is_trusted_when_the_mode_matches(self):
        assert leg(geometry(fingerprint_of=None)).has_current_geometry(span()) is True

    def test_a_leg_with_no_fingerprint_is_stale_when_the_mode_does_not(self):
        old = leg(
            geometry(intent=LegIntent.UNPAVED, fingerprint_of=None),
            intent=LegIntent.TWISTY_PAVED,
        )
        assert old.has_current_geometry(span()) is False

    def test_without_a_fingerprint_a_moved_waypoint_cannot_be_detected(self):
        """Stated so nobody reads the fallback as equivalent. It is weaker, deliberately."""
        assert leg(geometry(fingerprint_of=None)).has_current_geometry(span(end=ELSEWHERE)) is True


class TestTheSpanItIsGiven:
    @pytest.mark.parametrize("points", [0, 1])
    def test_too_few_points_is_not_current_rather_than_an_error(self, points):
        """A rebuild can hand it a span that no longer exists; refusing is the safe answer."""
        assert leg(geometry()).has_current_geometry(span()[:points]) is False

    def test_a_span_of_more_than_two_is_compared_whole(self):
        middle = Coordinate(lat=47.2, lon=-120.8)
        through = (
            Waypoint(coordinate=START),
            Waypoint(coordinate=middle),
            Waypoint(coordinate=END),
        )
        routed = geometry(fingerprint_of=(START, middle, END))
        assert leg(routed, end_waypoint_index=2).has_current_geometry(through) is True
        assert leg(geometry()).has_current_geometry(through) is False
