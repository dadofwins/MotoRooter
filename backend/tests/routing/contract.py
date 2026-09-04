"""Shared contract every `RoutingProvider` must satisfy.

Subclass `RoutingProviderContract` in an adapter's test module and override the
`provider` fixture. If an adapter passes this suite, the policy resolver can substitute
it for any other without callers noticing — which is the whole point of the pluggable
design. Adding a guarantee here holds every present and future adapter to it at once.

Real engines snap waypoints to the nearest routable way, so endpoint assertions use a
generous tolerance rather than exact equality.
"""

import inspect

import pytest

from motorooter.routing.errors import InvalidRequest, NoRouteFound, RoutingError
from motorooter.routing.geo import haversine_m
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    ProviderCapabilities,
    RouteLeg,
    RouteRequest,
)
from motorooter.routing.protocol import RoutingProvider

SNAP_TOLERANCE_M = 2_000.0
"""How far an engine may move a waypoint to reach a routable way."""


class _DegenerateNotDeclared:
    """Sentinel: the adapter has not said what its upstream does with a zero-length route."""


class DegenerateRouteContract:
    """What every adapter owes a zero-length route.

    Split out only for readability; `RoutingProviderContract` includes it, so an adapter
    that clears the suite clears this too.

    A rider asking for a loop is the ordinary way to reach it. "Three days of dirt starting
    and ending in Leavenworth" makes the trip briefly [Leavenworth, Leavenworth], and a leg
    whose two ends are the same coordinate is a legitimate question with a degenerate
    answer. Two adapters disagreed about it and the disagreement shipped: Google guarded,
    ORS did not, so ORS turned a one-point reply into a pydantic validation error relabelled
    as an unparseable response — and, being marked retryable, spent three metered requests
    re-asking a question whose answer cannot change.
    """

    @pytest.fixture
    def coincident_request(self) -> RouteRequest:
        """Both ends the same place. Override only if the coordinate must be routable."""
        point = Coordinate(lat=47.5962, lon=-120.6615)  # Leavenworth, WA
        return RouteRequest(waypoints=(point, point), intent=LegIntent.UNPAVED)

    @pytest.fixture
    def degenerate_upstream(self) -> RoutingProvider | None:
        """A provider whose upstream answers a coincident request with a single point.

        There is no default, deliberately. Override it with such a provider, or with `None`
        to declare that this provider synthesizes its own geometry and so can never receive
        a degenerate reply. Both are answers; not having one is what shipped last time.
        """
        return _DegenerateNotDeclared()  # type: ignore[return-value]

    async def test_a_coincident_span_never_leaks_a_non_routing_error(
        self, provider, coincident_request
    ):
        """Both ends the same point is a question, not a crash.

        Answering it with a leg is as valid as refusing it; what is not valid is a
        `ValidationError` from building the leg reaching a caller as itself.
        """
        try:
            leg = await provider.route(coincident_request)
        except RoutingError:
            return
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"leaked non-RoutingError: {type(exc).__name__}: {exc}")
        assert len(leg.geometry) >= 2, "a leg must have two ends even when they coincide"

    async def test_refusing_a_coincident_span_is_final_not_retryable(
        self, provider, coincident_request
    ):
        """Retrying cannot change the answer, and each attempt is a metered request."""
        try:
            await provider.route(coincident_request)
        except RoutingError as exc:
            assert not exc.retryable, f"{type(exc).__name__} would burn quota re-asking"

    async def test_a_degenerate_reply_is_no_route_found(
        self, degenerate_upstream, coincident_request
    ):
        """One point back from upstream is an answer about the road, not a broken payload.

        `NoRouteFound` rather than `ProviderUnavailable` because it is deterministic, which
        is what stops `RetryingProvider` paying for it three times.
        """
        if isinstance(degenerate_upstream, _DegenerateNotDeclared):
            pytest.fail(
                "override the `degenerate_upstream` fixture: return a provider whose upstream "
                "replies with one point, or None if this provider builds its own geometry"
            )
        if degenerate_upstream is None:
            pytest.skip("provider synthesizes its geometry; no upstream reply to degenerate")
        with pytest.raises(NoRouteFound):
            await degenerate_upstream.route(coincident_request)


class RoutingProviderContract(DegenerateRouteContract):
    """Behavioral guarantees shared by all providers."""

    @pytest.fixture
    def provider(self) -> RoutingProvider:
        raise NotImplementedError("override the `provider` fixture")

    @pytest.fixture
    def routable_request(self) -> RouteRequest:
        """A request this provider can satisfy. Override if fixtures need specific coords."""
        return RouteRequest(
            waypoints=(
                Coordinate(lat=45.5152, lon=-122.6784),
                Coordinate(lat=45.3311, lon=-121.7113),
            ),
            intent=LegIntent.UNPAVED,
        )

    def test_satisfies_the_protocol(self, provider):
        assert isinstance(provider, RoutingProvider)

    def test_route_is_async(self, provider):
        assert inspect.iscoroutinefunction(provider.route)

    def test_capabilities_are_well_formed(self, provider):
        caps = provider.capabilities
        assert isinstance(caps, ProviderCapabilities)
        assert caps.name

    def test_capabilities_are_stable_across_reads(self, provider):
        """Callers cache capabilities; they must not vary between calls."""
        assert provider.capabilities == provider.capabilities

    async def test_returns_a_route_leg(self, provider, routable_request):
        leg = await provider.route(routable_request)
        assert isinstance(leg, RouteLeg)

    async def test_leg_is_tagged_with_the_provider_name(self, provider, routable_request):
        """Legs must be traceable to their engine for debugging and re-routing."""
        leg = await provider.route(routable_request)
        assert leg.provider == provider.capabilities.name

    async def test_leg_preserves_requested_intent(self, provider, routable_request):
        leg = await provider.route(routable_request)
        assert leg.intent is routable_request.intent

    async def test_geometry_spans_the_requested_endpoints(self, provider, routable_request):
        leg = await provider.route(routable_request)
        assert haversine_m(leg.geometry[0], routable_request.waypoints[0]) < SNAP_TOLERANCE_M
        assert haversine_m(leg.geometry[-1], routable_request.waypoints[-1]) < SNAP_TOLERANCE_M

    async def test_reports_positive_distance_and_duration(self, provider, routable_request):
        leg = await provider.route(routable_request)
        assert leg.distance_m > 0
        assert leg.duration_s > 0

    async def test_surface_spans_stay_within_geometry(self, provider, routable_request):
        leg = await provider.route(routable_request)
        limit = len(leg.geometry) - 1
        assert all(s.end_index <= limit for s in leg.surface_spans)

    async def test_is_deterministic_for_the_same_request(self, provider, routable_request):
        """Required for caching to be sound."""
        first = await provider.route(routable_request)
        second = await provider.route(routable_request)
        assert first.geometry == second.geometry

    async def test_rejects_too_many_waypoints_as_invalid_request(self, provider):
        """Must fail before the network call, not surface an opaque upstream 4xx."""
        n = provider.capabilities.max_waypoints + 1
        oversized = RouteRequest(
            waypoints=tuple(Coordinate(lat=45.0 + i * 0.01, lon=-121.0) for i in range(n)),
            intent=LegIntent.UNPAVED,
        )
        with pytest.raises(InvalidRequest):
            await provider.route(oversized)

    async def test_raises_only_routing_errors(self, provider):
        """No provider-specific exception may reach a caller."""
        unroutable = RouteRequest(
            waypoints=(
                Coordinate(lat=-77.85, lon=166.67),  # Antarctica
                Coordinate(lat=-77.86, lon=166.68),
            ),
            intent=LegIntent.TECHNICAL_OFFROAD,
        )
        try:
            await provider.route(unroutable)
        except RoutingError:
            pass
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"leaked non-RoutingError: {type(exc).__name__}: {exc}")
