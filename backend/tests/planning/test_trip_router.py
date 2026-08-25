"""Routing a whole trip's legs through their resolved providers.

Three things this has to get right, all of them about *not* being all-or-nothing:

- **Per-leg provider choice.** A trip mixing highway connectors and dirt must reach two
  different engines, driven by the leg's own intent, never by a branch here.
- **Partial failure.** One unroutable dirt leg must not discard the nine legs that
  succeeded. On a metered free tier, throwing away successful responses throws away quota.
- **Incremental re-routing.** The fast path re-routes one leg and leaves its neighbours
  alone; replan can skip legs that are already routed.
"""

import asyncio

import pytest

from motorooter.api.error_codes import ErrorCode
from motorooter.planning.trip_router import TripRouter
from motorooter.routing.errors import NoRouteFound, ProviderUnavailable, RouteIncomplete
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    ProviderCapabilities,
    Surface,
    SurfaceSpan,
)
from motorooter.routing.policy import IntentPolicy, PolicyResolver
from motorooter.routing.providers.fake import FakeProvider
from motorooter.routing.registry import ProviderRegistry
from motorooter.trips.models import Trip, TripLeg, Waypoint
from tests.trips.store_contract import T0


def waypoint(lat: float) -> Waypoint:
    return Waypoint(coordinate=Coordinate(lat=lat, lon=-121.0))


def trip(*legs: TripLeg, waypoints: int = 0) -> Trip:
    """A trip whose waypoints march north, one per leg boundary."""
    count = waypoints or max((leg.end_waypoint_index for leg in legs), default=0) + 1
    return Trip(
        slug="oregon-backcountry",
        name="Oregon Backcountry",
        created_at=T0,
        edited_at=T0,
        waypoints=tuple(waypoint(45.0 + index) for index in range(count)),
        legs=legs,
    )


def leg(start: int, end: int, intent: LegIntent = LegIntent.UNPAVED, **overrides) -> TripLeg:
    return TripLeg(intent=intent, start_waypoint_index=start, end_waypoint_index=end, **overrides)


def build_resolver(
    *,
    dirt: FakeProvider | None = None,
    road: FakeProvider | None = None,
) -> PolicyResolver:
    """Two engines, so per-intent dispatch is observable rather than assumed."""
    dirt = dirt or FakeProvider(
        capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True)
    )
    road = road or FakeProvider(capabilities=ProviderCapabilities(name="road-engine"))
    registry = ProviderRegistry([dirt, road])
    return PolicyResolver(
        registry,
        {
            LegIntent.UNPAVED: IntentPolicy(provider="dirt-engine", requires_unpaved=True),
            LegIntent.TECHNICAL_OFFROAD: IntentPolicy(
                provider="dirt-engine", requires_unpaved=True
            ),
            LegIntent.HIGHWAY_CONNECTOR: IntentPolicy(provider="road-engine"),
            LegIntent.TWISTY_PAVED: IntentPolicy(provider="road-engine"),
            LegIntent.MANUAL_TRACK: IntentPolicy(provider="road-engine"),
        },
    )


@pytest.fixture
def dirt():
    return FakeProvider(capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True))


@pytest.fixture
def road():
    return FakeProvider(capabilities=ProviderCapabilities(name="road-engine"))


@pytest.fixture
def router(dirt, road):
    return TripRouter(build_resolver(dirt=dirt, road=road))


class TestRoutingEveryLeg:
    async def test_populates_routed_geometry_on_each_leg(self, router):
        result = await router.route_trip(trip(leg(0, 1), leg(1, 2)))
        assert all(routed.routed is not None for routed in result.trip.legs)

    async def test_reports_no_failures_when_everything_routes(self, router):
        result = await router.route_trip(trip(leg(0, 1)))
        assert result.failures == ()
        assert result.is_complete is True

    async def test_leaves_waypoints_and_identity_untouched(self, router):
        original = trip(leg(0, 1))
        result = await router.route_trip(original)
        assert result.trip.waypoints == original.waypoints
        assert result.trip.slug == original.slug

    async def test_does_not_stamp_edited_at(self, router):
        """Routing is not a user edit; bumping it would spuriously mark discovery stale."""
        original = trip(leg(0, 1))
        result = await router.route_trip(original)
        assert result.trip.edited_at == original.edited_at


class TestPerLegProviderChoice:
    async def test_each_leg_reaches_the_engine_its_intent_resolves_to(self, router, dirt, road):
        await router.route_trip(
            trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR), leg(1, 2, LegIntent.UNPAVED))
        )
        assert road.call_count == 1
        assert dirt.call_count == 1

    async def test_the_routed_leg_records_which_engine_produced_it(self, router):
        """A leg records its provider so re-routing one never disturbs its neighbours."""
        result = await router.route_trip(
            trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR), leg(1, 2, LegIntent.UNPAVED))
        )
        providers = [
            routed.routed.provider for routed in result.trip.legs if routed.routed is not None
        ]
        assert providers == ["road-engine", "dirt-engine"]

    async def test_a_provider_override_wins_over_the_policy_table(self, router, road):
        await router.route_trip(trip(leg(0, 1, LegIntent.UNPAVED, provider_override="road-engine")))
        assert road.call_count == 1

    async def test_a_leg_spanning_several_waypoints_sends_them_all_as_via_points(
        self, router, dirt
    ):
        await router.route_trip(trip(leg(0, 3)))
        assert len(dirt.calls[0].waypoints) == 4

    async def test_each_request_carries_only_its_own_leg_waypoints(self, router, dirt):
        await router.route_trip(trip(leg(0, 1), leg(1, 2)))
        assert [len(call.waypoints) for call in dirt.calls] == [2, 2]

    async def test_leg_requests_use_the_legs_intent(self, router, dirt):
        await router.route_trip(trip(leg(0, 1, LegIntent.TECHNICAL_OFFROAD)))
        assert dirt.calls[0].intent is LegIntent.TECHNICAL_OFFROAD


class TestPartialFailure:
    """One bad leg must not discard the others, or the quota they cost."""

    @pytest.fixture
    def broken_dirt(self):
        return FakeProvider(
            capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True),
            error=NoRouteFound("no dirt road connects those points", provider="dirt-engine"),
        )

    @pytest.fixture
    def half_broken(self, broken_dirt, road):
        return TripRouter(build_resolver(dirt=broken_dirt, road=road))

    async def test_the_successful_legs_are_still_routed(self, half_broken):
        result = await half_broken.route_trip(
            trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR), leg(1, 2, LegIntent.UNPAVED))
        )
        assert result.trip.legs[0].routed is not None

    async def test_the_failed_leg_is_reported_not_raised(self, half_broken):
        result = await half_broken.route_trip(
            trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR), leg(1, 2, LegIntent.UNPAVED))
        )
        assert [failure.leg_index for failure in result.failures] == [1]

    async def test_the_failure_carries_a_machine_readable_code(self, half_broken):
        result = await half_broken.route_trip(trip(leg(0, 1, LegIntent.UNPAVED)))
        assert result.failures[0].code == "no_route_found"

    async def test_the_failure_carries_the_engine_that_refused(self, half_broken):
        result = await half_broken.route_trip(trip(leg(0, 1, LegIntent.UNPAVED)))
        assert "dirt-engine" in result.failures[0].detail

    async def test_the_result_reports_itself_incomplete(self, half_broken):
        result = await half_broken.route_trip(trip(leg(0, 1, LegIntent.UNPAVED)))
        assert result.is_complete is False

    async def test_a_failed_leg_keeps_the_geometry_it_already_had(self, half_broken, road):
        """Losing a previously good route because a re-route failed is a downgrade."""
        first = await TripRouter(build_resolver(road=road)).route_trip(
            trip(leg(0, 1, LegIntent.UNPAVED))
        )
        retried = await half_broken.route_trip(first.trip)
        assert retried.trip.legs[0].routed == first.trip.legs[0].routed
        assert retried.failures != ()

    async def test_every_leg_failing_is_still_a_result_not_an_exception(self, half_broken):
        result = await half_broken.route_trip(trip(leg(0, 1), leg(1, 2)))
        assert len(result.failures) == 2
        assert result.trip.legs[0].routed is None

    async def test_an_unexpected_adapter_error_does_not_discard_the_successful_legs(self, road):
        """An adapter raising something that is not a RoutingError is still a leg failure.

        Raising out of `route_trip` here contradicted the quota rationale the whole contract
        rests on: leg 0's response was already paid for and got thrown away.
        """
        broken = FakeProvider(
            capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True),
            error=TimeoutError("adapter forgot to translate this"),  # type: ignore[arg-type]
        )
        router = TripRouter(build_resolver(dirt=broken, road=road))
        result = await router.route_trip(
            trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR), leg(1, 2, LegIntent.UNPAVED))
        )
        assert result.trip.legs[0].routed is not None
        assert [failure.leg_index for failure in result.failures] == [1]

    async def test_an_unexpected_adapter_error_is_reported_as_an_internal_error(self, road):
        """It has no ERROR_TABLE entry of its own, so it must not reach the wire unshaped."""
        broken = FakeProvider(
            capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True),
            error=TimeoutError("adapter forgot to translate this"),  # type: ignore[arg-type]
        )
        router = TripRouter(build_resolver(dirt=broken, road=road))
        result = await router.route_trip(trip(leg(0, 1, LegIntent.UNPAVED)))
        assert result.failures[0].code == ErrorCode.INTERNAL_ERROR.value
        assert result.failures[0].retryable is False

    async def test_route_leg_also_reports_an_untranslated_error_rather_than_raising(self):
        """`route_leg` calls the routing path directly, with no gather to absorb it.

        Catching only RoutingError there let an untranslated adapter error escape the
        fast path as an unshaped 500 mid-drag.
        """
        broken = FakeProvider(
            capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True),
            error=TimeoutError("adapter forgot to translate this"),  # type: ignore[arg-type]
        )
        router = TripRouter(build_resolver(dirt=broken))
        result = await router.route_leg(trip(leg(0, 1, LegIntent.UNPAVED)), 0)
        assert result.failures[0].code == ErrorCode.INTERNAL_ERROR.value

    async def test_cancellation_is_not_swallowed_as_a_leg_failure(self, road):
        """Shutdown must actually stop work, not be filed as "this leg did not route"."""
        cancelling = FakeProvider(
            capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True),
            error=asyncio.CancelledError(),  # type: ignore[arg-type]
        )
        router = TripRouter(build_resolver(dirt=cancelling, road=road))
        with pytest.raises(asyncio.CancelledError):
            await router.route_trip(trip(leg(0, 1, LegIntent.UNPAVED)))

    async def test_a_transient_failure_is_distinguishable_from_a_definitive_one(self, road):
        """The caller needs to know whether retrying is worth anything."""
        flaky = FakeProvider(
            capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True),
            error=ProviderUnavailable("upstream 503", provider="dirt-engine"),
        )
        router = TripRouter(build_resolver(dirt=flaky, road=road))
        result = await router.route_trip(trip(leg(0, 1, LegIntent.UNPAVED)))
        assert result.failures[0].retryable is True


class TestIncrementalRouting:
    async def test_routing_one_leg_leaves_its_neighbours_alone(self, router):
        routed = (await router.route_trip(trip(leg(0, 1), leg(1, 2)))).trip
        untouched = routed.legs[0].routed

        edited = await router.route_leg(routed, 1)
        assert edited.trip.legs[0].routed == untouched

    async def test_routing_one_leg_only_calls_one_engine(self, router, dirt):
        routed = (await router.route_trip(trip(leg(0, 1), leg(1, 2)))).trip
        dirt.calls.clear()
        await router.route_leg(routed, 1)
        assert dirt.call_count == 1

    async def test_routing_an_out_of_range_leg_is_an_error(self, router):
        with pytest.raises(IndexError):
            await router.route_leg(trip(leg(0, 1)), 5)

    async def test_a_negative_index_is_rejected_rather_than_wrapping(self, router, dirt):
        """Python would route the last leg and then write the result to a slot nobody reads
        — quota spent, trip unchanged, no error. Reject it instead."""
        with pytest.raises(IndexError):
            await router.route_leg(trip(leg(0, 1), leg(1, 2)), -1)
        assert dirt.call_count == 0

    async def test_only_unrouted_skips_legs_that_already_have_geometry(self, router, dirt):
        routed = (await router.route_trip(trip(leg(0, 1), leg(1, 2)))).trip
        dirt.calls.clear()
        await router.route_trip(routed, only_unrouted=True)
        assert dirt.call_count == 0

    async def test_only_unrouted_still_routes_the_legs_that_lack_geometry(self, router, dirt):
        partial = (await router.route_trip(trip(leg(0, 1)))).trip
        extended = partial.model_copy(
            update={
                "waypoints": (*partial.waypoints, waypoint(47.0)),
                "legs": (*partial.legs, leg(1, 2)),
            }
        )
        dirt.calls.clear()
        await router.route_trip(extended, only_unrouted=True)
        assert dirt.call_count == 1


class TestStitchingATrip:
    async def test_returns_one_continuous_geometry(self, router):
        routed = (await router.route_trip(trip(leg(0, 1), leg(1, 2)))).trip
        route = router.stitch_trip(routed)
        assert route.geometry[0] == routed.waypoints[0].coordinate
        assert route.geometry[-1] == routed.waypoints[-1].coordinate

    async def test_adjacent_legs_sharing_a_waypoint_join_without_a_gap(self, router):
        """FakeProvider emits waypoints exactly, so a clean join is the expected result."""
        routed = (await router.route_trip(trip(leg(0, 1), leg(1, 2)))).trip
        assert router.stitch_trip(routed).is_continuous is True

    async def test_surface_spans_survive_the_join(self, road):
        spanned = FakeProvider(
            capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True),
            points_per_segment=4,
            surface_spans=(SurfaceSpan(start_index=0, end_index=4, surface=Surface.UNPAVED),),
        )
        routed = (
            await TripRouter(build_resolver(dirt=spanned, road=road)).route_trip(
                trip(leg(0, 1), leg(1, 2))
            )
        ).trip
        route = TripRouter(build_resolver(dirt=spanned, road=road)).stitch_trip(routed)
        assert len(route.surface_spans) == 2
        assert route.unpaved_fraction == pytest.approx(1.0, rel=1e-6)


class TestStaleGeometryCannotBeStitchedSilently:
    """The executed failure: a leg whose re-route failed keeps its previous geometry.

    `RouteIncomplete` only ever fired on `routed is None`, so `stitch_trip` succeeded on
    exactly the trip `is_complete` had just called incomplete. A rider retags a leg as dirt,
    the dirt engine is down, and the export renders perfectly at 0% unpaved — carrying the
    paved geometry of the leg they replaced.
    """

    @pytest.fixture
    def road(self):
        return FakeProvider(capabilities=ProviderCapabilities(name="road-engine"))

    @pytest.fixture
    async def paved_trip(self, road):
        """Two highway legs, routed and cached."""
        router = TripRouter(build_resolver(road=road))
        return (
            await router.route_trip(
                trip(
                    leg(0, 1, LegIntent.HIGHWAY_CONNECTOR),
                    leg(1, 2, LegIntent.HIGHWAY_CONNECTOR),
                )
            )
        ).trip

    @pytest.fixture
    def down(self, road):
        broken = FakeProvider(
            capabilities=ProviderCapabilities(name="dirt-engine", prefers_unpaved=True),
            error=ProviderUnavailable("upstream 503", provider="dirt-engine"),
        )
        return TripRouter(build_resolver(dirt=broken, road=road))

    async def test_the_retagged_leg_keeps_its_stale_geometry(self, paved_trip, down):
        """Deliberate — losing a good route to a failed retry is a downgrade. But detectable."""
        retagged = paved_trip.model_copy(
            update={
                "legs": (
                    paved_trip.legs[0],
                    paved_trip.legs[1].model_copy(update={"intent": LegIntent.UNPAVED}),
                )
            }
        )
        result = await down.route_trip(retagged)
        assert result.is_complete is False
        assert result.trip.legs[1].routed is not None

    async def test_stitching_that_trip_is_refused(self, paved_trip, down):
        retagged = paved_trip.model_copy(
            update={
                "legs": (
                    paved_trip.legs[0],
                    paved_trip.legs[1].model_copy(update={"intent": LegIntent.UNPAVED}),
                )
            }
        )
        result = await down.route_trip(retagged)
        with pytest.raises(RouteIncomplete):
            down.stitch_trip(result.trip)

    async def test_the_refusal_says_the_geometry_is_stale_not_missing(self, paved_trip, down):
        retagged = paved_trip.model_copy(
            update={
                "legs": (
                    paved_trip.legs[0],
                    paved_trip.legs[1].model_copy(update={"intent": LegIntent.UNPAVED}),
                )
            }
        )
        result = await down.route_trip(retagged)
        with pytest.raises(RouteIncomplete, match="stale"):
            down.stitch_trip(result.trip)

    async def test_stitching_a_result_refuses_a_failure_the_trip_alone_cannot_show(self):
        """The case the intent check cannot see, and the reason `stitch_result` exists.

        A leg re-routed for the *same* intent by the *same* engine, where the engine 503s.
        Its cached geometry still matches its intent and provider, so `stitch_trip` has
        nothing to detect — but the waypoints may have moved, and the result knows the
        re-route failed. `is_complete` is the only signal, so it must not be optional.
        """
        healthy = FakeProvider(capabilities=ProviderCapabilities(name="road-engine"))
        routed = (
            await TripRouter(build_resolver(road=healthy)).route_trip(
                trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR))
            )
        ).trip

        flaky = FakeProvider(
            capabilities=ProviderCapabilities(name="road-engine"),
            error=ProviderUnavailable("upstream 503", provider="road-engine"),
        )
        router = TripRouter(build_resolver(road=flaky))
        result = await router.route_trip(routed)

        assert result.is_complete is False
        # Nothing about the trip itself looks wrong — this is the gap being closed.
        assert router.stitch_trip(result.trip).is_continuous is True
        with pytest.raises(RouteIncomplete):
            router.stitch_result(result)

    async def test_stitching_a_complete_result_works(self, road):
        router = TripRouter(build_resolver(road=road))
        result = await router.route_trip(
            trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR), leg(1, 2, LegIntent.HIGHWAY_CONNECTOR))
        )
        assert len(router.stitch_result(result).geometry) > 0

    async def test_a_freshly_routed_trip_stitches_normally(self, road):
        """The check must not reject legs that are genuinely current."""
        router = TripRouter(build_resolver(road=road))
        routed = (
            await router.route_trip(
                trip(
                    leg(0, 1, LegIntent.HIGHWAY_CONNECTOR),
                    leg(1, 2, LegIntent.HIGHWAY_CONNECTOR),
                )
            )
        ).trip
        assert router.stitch_trip(routed).is_continuous is True

    async def test_a_provider_override_change_also_invalidates_the_geometry(self, road):
        router = TripRouter(build_resolver(road=road))
        routed = (await router.route_trip(trip(leg(0, 1, LegIntent.HIGHWAY_CONNECTOR)))).trip
        repinned = routed.model_copy(
            update={
                "legs": (routed.legs[0].model_copy(update={"provider_override": "dirt-engine"}),)
            }
        )
        with pytest.raises(RouteIncomplete, match="stale"):
            router.stitch_trip(repinned)

    async def test_stale_geometry_can_be_stitched_when_the_caller_says_so(self, paved_trip, down):
        """An escape hatch for showing the user what they currently have. Never the default."""
        result = await down.route_trip(
            paved_trip.model_copy(
                update={
                    "legs": (
                        paved_trip.legs[0],
                        paved_trip.legs[1].model_copy(update={"intent": LegIntent.UNPAVED}),
                    )
                }
            )
        )
        assert down.stitch_trip(result.trip, allow_stale=True).geometry != ()


class TestStitchingATripContinued:
    async def test_stitching_a_partially_routed_trip_is_refused(self, router):
        """Silently omitting an unrouted leg would produce a shorter route that looks whole."""
        half = trip(leg(0, 1), leg(1, 2))
        routed_first = await router.route_leg(half, 0)
        with pytest.raises(RouteIncomplete):
            router.stitch_trip(routed_first.trip)

    async def test_the_refusal_names_the_unrouted_legs(self, router):
        with pytest.raises(RouteIncomplete, match="1"):
            router.stitch_trip((await router.route_leg(trip(leg(0, 1), leg(1, 2)), 0)).trip)

    async def test_stitching_a_trip_with_no_legs_gives_an_empty_route(self, router):
        assert router.stitch_trip(trip(waypoints=1)).geometry == ()
