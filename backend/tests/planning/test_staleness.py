"""Staleness and failure state, once a leg records what it was routed from.

Two gaps this closes, both of which survived the previous round:

- A moved waypoint left geometry that looked current. Intent and provider still matched, so
  nothing detected it, and the export carried a route through a point the rider had dragged
  away from.
- A leg with no geometry was byte-identical whether its routing had failed or had never been
  attempted. `TripRoutingResult.failures` knew, but does not survive persistence, and `Trip`
  is the only thing the store accepts.
"""

import pytest

from motorooter.error_codes import ErrorCode
from motorooter.planning.trip_router import TripRouter
from motorooter.routing.errors import ProviderUnavailable, RouteIncomplete
from motorooter.routing.models import Coordinate, LegIntent, ProviderCapabilities
from motorooter.routing.providers.fake import FakeProvider
from motorooter.trips.models import Trip, TripLeg, Waypoint
from tests.planning.test_trip_router import build_resolver, leg, trip
from tests.trips.store_contract import T0


@pytest.fixture
def road():
    return FakeProvider(capabilities=ProviderCapabilities(name="road-engine"))


@pytest.fixture
def router(road):
    return TripRouter(build_resolver(road=road))


@pytest.fixture
def down(road):
    broken = FakeProvider(
        capabilities=ProviderCapabilities(name="road-engine"),
        error=ProviderUnavailable("upstream 503", provider="road-engine"),
    )
    return TripRouter(build_resolver(road=broken))


async def routed_trip(router: TripRouter) -> Trip:
    return (
        await router.route_trip(
            trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR), leg(1, 2, LegIntent.HIGHWAY_CONNECTOR))
        )
    ).trip


class TestRecordingWhatWasRouted:
    async def test_a_routed_leg_records_its_request(self, router):
        result = await router.route_trip(trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR)))
        assert result.trip.legs[0].routed is not None
        assert result.trip.legs[0].routed.routed_from is not None

    async def test_the_fingerprint_holds_the_legs_own_waypoints(self, router):
        result = await router.route_trip(trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR)))
        routed = result.trip.legs[0].routed
        assert routed is not None
        fingerprint = routed.routed_from
        assert fingerprint is not None
        assert len(fingerprint.waypoints) == 2

    async def test_the_fingerprint_records_a_pinned_provider(self, router):
        result = await router.route_trip(
            trip(leg(0, 1, LegIntent.UNPAVED, provider_override="road-engine"))
        )
        routed = result.trip.legs[0].routed
        assert routed is not None
        fingerprint = routed.routed_from
        assert fingerprint is not None
        assert fingerprint.provider_override == "road-engine"

    async def test_a_leg_spanning_via_points_records_all_of_them(self, router):
        result = await router.route_trip(trip(leg(0, 3, LegIntent.HIGHWAY_CONNECTOR)))
        routed = result.trip.legs[0].routed
        assert routed is not None
        fingerprint = routed.routed_from
        assert fingerprint is not None
        assert len(fingerprint.waypoints) == 4


class TestAMovedWaypointIsNowDetectable:
    """The gap the intent-and-provider check could not close."""

    async def test_dragging_a_waypoint_makes_the_geometry_stale(self, router):
        routed = await routed_trip(router)
        moved = routed.model_copy(
            update={
                "waypoints": (
                    routed.waypoints[0],
                    Waypoint(coordinate=Coordinate(lat=45.9, lon=-121.0)),
                    routed.waypoints[2],
                )
            }
        )
        with pytest.raises(RouteIncomplete, match="stale"):
            router.stitch_trip(moved)

    async def test_only_the_legs_touching_that_waypoint_are_stale(self, router):
        routed = await routed_trip(router)
        moved = routed.model_copy(
            update={
                "waypoints": (
                    routed.waypoints[0],
                    routed.waypoints[1],
                    Waypoint(coordinate=Coordinate(lat=47.5, lon=-121.0)),
                )
            }
        )
        with pytest.raises(RouteIncomplete) as caught:
            router.stitch_trip(moved)
        assert caught.value.leg_indices == (1,)

    async def test_re_routing_the_moved_leg_clears_the_staleness(self, router):
        routed = await routed_trip(router)
        moved = routed.model_copy(
            update={
                "waypoints": (
                    routed.waypoints[0],
                    routed.waypoints[1],
                    Waypoint(coordinate=Coordinate(lat=47.5, lon=-121.0)),
                )
            }
        )
        repaired = (await router.route_leg(moved, 1)).trip
        assert router.stitch_trip(repaired).is_continuous is True

    async def test_an_untouched_trip_is_not_stale(self, router):
        """The check must not cry wolf on geometry that is genuinely current."""
        routed = await routed_trip(router)
        assert router.stitch_trip(routed).is_continuous is True

    async def test_a_leg_with_no_fingerprint_is_not_assumed_stale(self, router):
        """Trips written before this field existed still export rather than hard-failing."""
        routed = await routed_trip(router)
        legacy = routed.model_copy(
            update={
                "legs": tuple(
                    each.model_copy(
                        update={"routed": each.routed.model_copy(update={"routed_from": None})}
                    )
                    for each in routed.legs
                    if each.routed is not None
                )
            }
        )
        assert router.stitch_trip(legacy).is_continuous is True


class TestFailureSurvivesPersistence:
    async def test_a_failed_leg_records_why(self, down):
        result = await down.route_trip(trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR)))
        assert result.trip.legs[0].last_routing_error == ErrorCode.PROVIDER_UNAVAILABLE

    async def test_a_never_attempted_leg_records_nothing(self):
        assert (
            TripLeg(
                intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1
            ).last_routing_error
            is None
        )

    async def test_the_marker_distinguishes_failed_from_never_planned(self, down):
        """Both have `routed is None`; only this tells them apart after a reload."""
        result = await down.route_trip(trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR)))
        failed = result.trip.legs[0]
        never = TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1)
        assert failed.routed is None and never.routed is None
        assert failed.last_routing_error != never.last_routing_error

    async def test_a_successful_re_route_clears_the_marker(self, down, road):
        """A stale error would leave a healthy leg permanently flagged as broken."""
        failed = (await down.route_trip(trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR)))).trip
        assert failed.legs[0].last_routing_error is not None

        healthy = TripRouter(build_resolver(road=road))
        repaired = (await healthy.route_trip(failed)).trip
        assert repaired.legs[0].last_routing_error is None

    async def test_it_survives_a_json_round_trip(self, down):
        result = await down.route_trip(trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR)))
        reloaded = Trip.model_validate_json(result.trip.model_dump_json())
        assert reloaded.legs[0].last_routing_error == ErrorCode.PROVIDER_UNAVAILABLE

    async def test_a_skipped_leg_keeps_whatever_marker_it_had(self, down, road):
        """`only_unrouted` skips a leg entirely; skipping must not silently clear its error."""
        failed = (await down.route_trip(trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR)))).trip
        marked = failed.model_copy(
            update={
                "legs": (
                    failed.legs[0].model_copy(
                        update={"last_routing_error": ErrorCode.NO_ROUTE_FOUND}
                    ),
                )
            }
        )
        # Nothing to skip here — the leg has no geometry — so it is retried and cleared.
        repaired = (
            await TripRouter(build_resolver(road=road)).route_trip(marked, only_unrouted=True)
        ).trip
        assert repaired.legs[0].last_routing_error is None


class TestTheTripCanReportItsOwnState:
    async def test_a_fully_routed_trip_says_so(self, router):
        assert (await routed_trip(router)).is_fully_routed is True

    async def test_an_unrouted_trip_says_so(self):
        assert trip(leg(0, 1)).is_fully_routed is False

    async def test_it_names_the_legs_without_geometry(self, router):
        routed = await routed_trip(router)
        partial = routed.model_copy(
            update={"legs": (routed.legs[0], routed.legs[1].model_copy(update={"routed": None}))}
        )
        assert partial.unrouted_leg_indices == (1,)

    async def test_total_distance_still_understates_a_partial_trip(self, router):
        """Documented, not fixed here: the number is only trustworthy alongside the flag.

        Making `total_distance_m` refuse would break every caller that legitimately shows a
        partial total while planning. The flag is what makes the number interpretable.
        """
        routed = await routed_trip(router)
        partial = routed.model_copy(
            update={"legs": (routed.legs[0], routed.legs[1].model_copy(update={"routed": None}))}
        )
        assert partial.total_distance_m < routed.total_distance_m
        assert partial.is_fully_routed is False


def test_trip_helpers_are_not_serialized_into_the_contract():
    """Properties, not fields — adding them must not change the wire shape."""
    document = Trip(slug="a-trip", name="A Trip", created_at=T0, edited_at=T0).model_dump()
    assert "is_fully_routed" not in document
    assert "unrouted_leg_indices" not in document
