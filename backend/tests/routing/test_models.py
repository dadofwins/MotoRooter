"""Domain models shared by every routing provider.

These are the types that cross the Python/TypeScript boundary, so their invariants
are enforced here rather than trusted from adapters.
"""

import pytest
from pydantic import ValidationError

from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    ProviderCapabilities,
    RouteLeg,
    RouteRequest,
    Surface,
    SurfaceSpan,
)


class TestCoordinate:
    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)],
    )
    def test_rejects_out_of_range(self, lat, lon):
        with pytest.raises(ValidationError):
            Coordinate(lat=lat, lon=lon)

    def test_accepts_extremes(self):
        assert Coordinate(lat=90.0, lon=180.0).lat == 90.0

    def test_geojson_order_is_lon_lat(self):
        """GeoJSON is x,y — transposing this silently puts routes in the ocean."""
        assert Coordinate(lat=45.5, lon=-121.7).to_geojson() == (-121.7, 45.5)

    def test_geojson_roundtrip(self):
        c = Coordinate(lat=45.5, lon=-121.7)
        assert Coordinate.from_geojson(c.to_geojson()) == c


class TestRouteRequest:
    def test_requires_at_least_two_waypoints(self):
        with pytest.raises(ValidationError):
            RouteRequest(waypoints=[Coordinate(lat=45.0, lon=-121.0)], intent=LegIntent.UNPAVED)

    def test_accepts_two_waypoints(self):
        req = RouteRequest(
            waypoints=[Coordinate(lat=45.0, lon=-121.0), Coordinate(lat=46.0, lon=-121.0)],
            intent=LegIntent.UNPAVED,
        )
        assert req.intent is LegIntent.UNPAVED

    def test_is_immutable(self):
        """Requests are cache keys; mutation after hashing would corrupt the cache."""
        req = RouteRequest(
            waypoints=[Coordinate(lat=45.0, lon=-121.0), Coordinate(lat=46.0, lon=-121.0)],
            intent=LegIntent.UNPAVED,
        )
        with pytest.raises(ValidationError):
            req.intent = LegIntent.TWISTY_PAVED  # type: ignore[misc]


class TestSurfaceSpan:
    def test_rejects_non_increasing_indices(self):
        with pytest.raises(ValidationError):
            SurfaceSpan(start_index=5, end_index=5, surface=Surface.UNPAVED)

    def test_rejects_negative_start(self):
        with pytest.raises(ValidationError):
            SurfaceSpan(start_index=-1, end_index=3, surface=Surface.PAVED)


class TestRouteLeg:
    @staticmethod
    def _leg(spans: list[SurfaceSpan]) -> RouteLeg:
        # Three 1-degree-latitude hops, ~111 km each, ~333 km total.
        geometry = [Coordinate(lat=45.0 + i, lon=-121.0) for i in range(4)]
        return RouteLeg(
            geometry=geometry,
            distance_m=333_585.0,
            duration_s=12_000.0,
            surface_spans=spans,
            provider="fake",
            intent=LegIntent.UNPAVED,
        )

    def test_requires_at_least_two_geometry_points(self):
        with pytest.raises(ValidationError):
            RouteLeg(
                geometry=[Coordinate(lat=45.0, lon=-121.0)],
                distance_m=0.0,
                duration_s=0.0,
                provider="fake",
                intent=LegIntent.UNPAVED,
            )

    def test_rejects_span_index_beyond_geometry(self):
        """An adapter emitting a span past the end must fail loudly, not slice silently."""
        with pytest.raises(ValidationError):
            self._leg([SurfaceSpan(start_index=0, end_index=99, surface=Surface.UNPAVED)])

    def test_unpaved_distance_sums_only_unpaved_spans(self):
        leg = self._leg(
            [
                SurfaceSpan(start_index=0, end_index=1, surface=Surface.PAVED),
                SurfaceSpan(start_index=1, end_index=3, surface=Surface.UNPAVED),
            ]
        )
        assert leg.unpaved_distance_m == pytest.approx(222_390, rel=0.001)

    def test_unpaved_fraction_is_relative_to_total(self):
        leg = self._leg(
            [
                SurfaceSpan(start_index=0, end_index=1, surface=Surface.PAVED),
                SurfaceSpan(start_index=1, end_index=3, surface=Surface.UNPAVED),
            ]
        )
        assert leg.unpaved_fraction == pytest.approx(2 / 3, rel=0.01)

    def test_unpaved_fraction_is_zero_when_surface_unknown(self):
        """No surface data must read as 0.0, never as a divide-by-zero or None."""
        assert self._leg([]).unpaved_fraction == 0.0

    def test_unknown_surface_does_not_count_as_unpaved(self):
        leg = self._leg([SurfaceSpan(start_index=0, end_index=3, surface=Surface.UNKNOWN)])
        assert leg.unpaved_distance_m == 0.0


class TestProviderCapabilities:
    def test_live_updates_enabled_when_interval_set(self):
        caps = ProviderCapabilities(name="google", live_update_interval_ms=1000)
        assert caps.supports_live_updates is True

    def test_none_interval_means_preview_only(self):
        """Expensive providers rubber-band during drag and route only on release."""
        caps = ProviderCapabilities(name="ors", live_update_interval_ms=None)
        assert caps.supports_live_updates is False

    def test_rejects_negative_interval(self):
        with pytest.raises(ValidationError):
            ProviderCapabilities(name="bad", live_update_interval_ms=-1)

    def test_max_waypoints_must_be_at_least_two(self):
        with pytest.raises(ValidationError):
            ProviderCapabilities(name="bad", max_waypoints=1)
