"""FakeProvider: the reference implementation and the reason the suite is hermetic."""

import pytest

from motorooter.routing.errors import InvalidRequest, ProviderUnavailable
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    ProviderCapabilities,
    RouteRequest,
    Surface,
    SurfaceSpan,
)
from motorooter.routing.providers.fake import FakeProvider
from tests.routing.contract import RoutingProviderContract


class TestFakeProviderContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self):
        return FakeProvider()


@pytest.fixture
def request_pdx_hood():
    return RouteRequest(
        waypoints=(
            Coordinate(lat=45.5152, lon=-122.6784),
            Coordinate(lat=45.3311, lon=-121.7113),
        ),
        intent=LegIntent.UNPAVED,
    )


class TestInterpolation:
    async def test_generates_requested_number_of_points(self, request_pdx_hood):
        leg = await FakeProvider(points_per_segment=10).route(request_pdx_hood)
        assert len(leg.geometry) == 11  # 10 segments between 2 waypoints

    async def test_passes_through_every_waypoint(self):
        waypoints = (
            Coordinate(lat=45.0, lon=-121.0),
            Coordinate(lat=45.5, lon=-121.5),
            Coordinate(lat=46.0, lon=-121.0),
        )
        leg = await FakeProvider(points_per_segment=4).route(
            RouteRequest(waypoints=waypoints, intent=LegIntent.UNPAVED)
        )
        for wp in waypoints:
            assert wp in leg.geometry

    async def test_distance_matches_generated_geometry(self, request_pdx_hood):
        leg = await FakeProvider().route(request_pdx_hood)
        assert leg.distance_m == pytest.approx(leg.geometry_length_m, rel=1e-6)


class TestCallRecording:
    """Decorator tests assert on these, so they are part of the fake's contract."""

    async def test_counts_calls(self, request_pdx_hood):
        provider = FakeProvider()
        await provider.route(request_pdx_hood)
        await provider.route(request_pdx_hood)
        assert provider.call_count == 2

    async def test_records_calls_in_order(self, request_pdx_hood):
        provider = FakeProvider()
        await provider.route(request_pdx_hood)
        assert provider.calls == [request_pdx_hood]

    async def test_rejected_requests_are_not_counted(self):
        """A request rejected before dispatch must not look like upstream traffic."""
        provider = FakeProvider(capabilities=ProviderCapabilities(name="fake", max_waypoints=2))
        oversized = RouteRequest(
            waypoints=tuple(Coordinate(lat=45.0 + i, lon=-121.0) for i in range(3)),
            intent=LegIntent.UNPAVED,
        )
        with pytest.raises(InvalidRequest):
            await provider.route(oversized)
        assert provider.call_count == 0


class TestFailureInjection:
    async def test_raises_the_configured_error(self, request_pdx_hood):
        provider = FakeProvider(error=ProviderUnavailable("boom", provider="fake"))
        with pytest.raises(ProviderUnavailable):
            await provider.route(request_pdx_hood)

    async def test_fails_only_the_first_n_calls(self, request_pdx_hood):
        """Lets retry tests assert recovery rather than just failure."""
        provider = FakeProvider(error=ProviderUnavailable("boom"), fail_first=2)
        for _ in range(2):
            with pytest.raises(ProviderUnavailable):
                await provider.route(request_pdx_hood)
        leg = await provider.route(request_pdx_hood)
        assert leg.distance_m > 0

    async def test_failed_calls_are_still_counted(self, request_pdx_hood):
        """Quota is consumed by failed upstream calls too."""
        provider = FakeProvider(error=ProviderUnavailable("boom"), fail_first=1)
        with pytest.raises(ProviderUnavailable):
            await provider.route(request_pdx_hood)
        assert provider.call_count == 1


class TestScriptedSurfaces:
    async def test_returns_configured_surface_spans(self, request_pdx_hood):
        provider = FakeProvider(
            points_per_segment=4,
            surface_spans=(SurfaceSpan(start_index=0, end_index=2, surface=Surface.UNPAVED),),
        )
        leg = await provider.route(request_pdx_hood)
        assert leg.unpaved_fraction == pytest.approx(0.5, rel=0.01)


class TestConfigurableCapabilities:
    async def test_capabilities_can_be_overridden(self):
        caps = ProviderCapabilities(name="pretend-ors", prefers_unpaved=True, max_waypoints=70)
        assert FakeProvider(capabilities=caps).capabilities == caps

    async def test_leg_provider_tag_follows_overridden_name(self, request_pdx_hood):
        provider = FakeProvider(capabilities=ProviderCapabilities(name="pretend-ors"))
        leg = await provider.route(request_pdx_hood)
        assert leg.provider == "pretend-ors"
