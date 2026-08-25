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

from motorooter.routing.errors import InvalidRequest, RoutingError
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


class RoutingProviderContract:
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
