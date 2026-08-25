"""Google Directions adapter. Fixture-driven; never hits the network."""

from typing import Any

import httpx
import pytest
import respx

from motorooter.routing.errors import (
    InvalidRequest,
    NoRouteFound,
    ProviderUnavailable,
    QuotaExceeded,
)
from motorooter.routing.models import Coordinate, LegIntent, RouteRequest
from motorooter.routing.providers.google import GOOGLE_DIRECTIONS_URL, GoogleDirectionsProvider
from motorooter.routing.providers.polyline import encode_polyline
from tests.routing.contract import RoutingProviderContract

DIRECTIONS_URL = respx.patterns.M(url__startswith=GOOGLE_DIRECTIONS_URL)


def google_response(
    step_polylines: list[str], *, distance: int = 95_000, duration: int = 5_400
) -> dict[str, Any]:
    return {
        "status": "OK",
        "routes": [
            {
                "legs": [
                    {
                        "distance": {"value": distance},
                        "duration": {"value": duration},
                        "steps": [{"polyline": {"points": p}} for p in step_polylines],
                    }
                ]
            }
        ],
    }


def echo_requested_coordinates(request: httpx.Request) -> httpx.Response:
    """Return a route running through exactly the requested origin and destination."""
    params = request.url.params
    points = [params["origin"], *params.get_list("waypoints"), params["destination"]]
    coords = []
    for raw in points:
        for part in raw.split("|"):
            lat, lon = part.removeprefix("via:").split(",")
            coords.append(Coordinate(lat=float(lat), lon=float(lon)))
    return httpx.Response(200, json=google_response([encode_polyline(coords)]))


@pytest.fixture
def mock_google():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def provider():
    return GoogleDirectionsProvider(api_key="test-key")


@pytest.fixture
def pdx_hood():
    return RouteRequest(
        waypoints=(
            Coordinate(lat=45.5152, lon=-122.6784),
            Coordinate(lat=45.3311, lon=-121.7113),
        ),
        intent=LegIntent.HIGHWAY_CONNECTOR,
    )


class TestGoogleContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self, mock_google):
        mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        return GoogleDirectionsProvider(api_key="test-key")

    @pytest.fixture
    def routable_request(self):
        return RouteRequest(
            waypoints=(
                Coordinate(lat=45.5152, lon=-122.6784),
                Coordinate(lat=45.3311, lon=-121.7113),
            ),
            intent=LegIntent.HIGHWAY_CONNECTOR,
        )


class TestRequestConstruction:
    async def test_sends_origin_and_destination_as_lat_lng(self, provider, mock_google, pdx_hood):
        route = mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(pdx_hood)
        params = route.calls.last.request.url.params
        assert params["origin"] == "45.5152,-122.6784"
        assert params["destination"] == "45.3311,-121.7113"

    async def test_intermediate_waypoints_are_via_points(self, provider, mock_google):
        """`via:` keeps them as shaping points instead of adding stopovers to the leg."""
        route = mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(
            RouteRequest(
                waypoints=(
                    Coordinate(lat=45.0, lon=-121.0),
                    Coordinate(lat=45.5, lon=-121.5),
                    Coordinate(lat=46.0, lon=-121.0),
                ),
                intent=LegIntent.HIGHWAY_CONNECTOR,
            )
        )
        assert route.calls.last.request.url.params["waypoints"] == "via:45.5,-121.5"

    async def test_sends_the_api_key(self, provider, mock_google, pdx_hood):
        route = mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(pdx_hood)
        assert route.calls.last.request.url.params["key"] == "test-key"

    async def test_avoidances_are_forwarded(self, provider, mock_google):
        route = mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(
            RouteRequest(
                waypoints=(Coordinate(lat=45.0, lon=-121.0), Coordinate(lat=46.0, lon=-121.0)),
                intent=LegIntent.TWISTY_PAVED,
                avoid_highways=True,
                avoid_ferries=True,
            )
        )
        assert route.calls.last.request.url.params["avoid"] == "highways|ferries"

    async def test_avoid_omitted_when_nothing_to_avoid(self, provider, mock_google, pdx_hood):
        route = mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(pdx_hood)
        assert "avoid" not in route.calls.last.request.url.params


class TestResponseParsing:
    async def test_concatenates_step_polylines(self, provider, mock_google, pdx_hood):
        first = encode_polyline(
            [Coordinate(lat=45.0, lon=-121.0), Coordinate(lat=45.5, lon=-121.0)]
        )
        second = encode_polyline(
            [Coordinate(lat=45.5, lon=-121.0), Coordinate(lat=46.0, lon=-121.0)]
        )
        mock_google.route(DIRECTIONS_URL).respond(json=google_response([first, second]))
        leg = await provider.route(pdx_hood)
        assert len(leg.geometry) == 3, "shared boundary point must not be duplicated"

    async def test_uses_full_step_geometry_not_the_simplified_overview(
        self, provider, mock_google, pdx_hood
    ):
        """The overview polyline is decimated; GPX export and drag splicing need detail."""
        detailed = encode_polyline([Coordinate(lat=45.0 + i * 0.01, lon=-121.0) for i in range(20)])
        body = google_response([detailed])
        body["routes"][0]["overview_polyline"] = {"points": encode_polyline([])}
        mock_google.route(DIRECTIONS_URL).respond(json=body)
        leg = await provider.route(pdx_hood)
        assert len(leg.geometry) == 20

    async def test_sums_distance_and_duration_across_legs(self, provider, mock_google, pdx_hood):
        body = google_response([encode_polyline([Coordinate(lat=45.0, lon=-121.0)])])
        body["routes"][0]["legs"].append(
            {
                "distance": {"value": 5_000},
                "duration": {"value": 300},
                "steps": [
                    {"polyline": {"points": encode_polyline([Coordinate(lat=46.0, lon=-121.0)])}}
                ],
            }
        )
        mock_google.route(DIRECTIONS_URL).respond(json=body)
        leg = await provider.route(pdx_hood)
        assert (leg.distance_m, leg.duration_s) == (100_000.0, 5_700.0)

    async def test_reports_no_surface_data(self, provider, mock_google, pdx_hood):
        """Google exposes no surface tags; claiming any would corrupt dirt statistics."""
        mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        leg = await provider.route(pdx_hood)
        assert leg.surface_spans == ()
        assert leg.unpaved_fraction == 0.0

    async def test_tags_the_leg_as_google(self, provider, mock_google, pdx_hood):
        mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        assert (await provider.route(pdx_hood)).provider == "google"

    async def test_malformed_payload_is_a_routing_error(self, provider, mock_google, pdx_hood):
        mock_google.route(DIRECTIONS_URL).respond(json={"status": "OK", "routes": [{}]})
        with pytest.raises(ProviderUnavailable):
            await provider.route(pdx_hood)


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("ZERO_RESULTS", NoRouteFound),
            ("NOT_FOUND", InvalidRequest),
            ("INVALID_REQUEST", InvalidRequest),
            ("MAX_WAYPOINTS_EXCEEDED", InvalidRequest),
            ("REQUEST_DENIED", InvalidRequest),
            ("OVER_QUERY_LIMIT", QuotaExceeded),
            ("OVER_DAILY_LIMIT", QuotaExceeded),
            ("UNKNOWN_ERROR", ProviderUnavailable),
        ],
    )
    async def test_maps_api_status_codes(self, provider, mock_google, pdx_hood, status, expected):
        """Google signals failure in the body with HTTP 200, so status must be inspected."""
        mock_google.route(DIRECTIONS_URL).respond(
            json={"status": status, "error_message": "upstream said no"}
        )
        with pytest.raises(expected):
            await provider.route(pdx_hood)

    async def test_unknown_error_is_retryable(self, provider, mock_google, pdx_hood):
        """Google documents UNKNOWN_ERROR as a transient server failure worth retrying."""
        mock_google.route(DIRECTIONS_URL).respond(json={"status": "UNKNOWN_ERROR"})
        with pytest.raises(ProviderUnavailable) as exc:
            await provider.route(pdx_hood)
        assert exc.value.retryable is True

    async def test_error_message_is_surfaced(self, provider, mock_google, pdx_hood):
        mock_google.route(DIRECTIONS_URL).respond(
            json={"status": "REQUEST_DENIED", "error_message": "API key not authorized"}
        )
        with pytest.raises(InvalidRequest, match="API key not authorized"):
            await provider.route(pdx_hood)

    async def test_http_5xx_is_provider_unavailable(self, provider, mock_google, pdx_hood):
        mock_google.route(DIRECTIONS_URL).respond(status_code=503)
        with pytest.raises(ProviderUnavailable):
            await provider.route(pdx_hood)

    async def test_timeout_is_provider_unavailable(self, provider, mock_google, pdx_hood):
        mock_google.route(DIRECTIONS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        with pytest.raises(ProviderUnavailable):
            await provider.route(pdx_hood)

    async def test_too_many_waypoints_rejected_before_dispatch(self, provider, mock_google):
        route = mock_google.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        n = provider.capabilities.max_waypoints + 1
        with pytest.raises(InvalidRequest):
            await provider.route(
                RouteRequest(
                    waypoints=tuple(Coordinate(lat=45.0 + i * 0.01, lon=-121.0) for i in range(n)),
                    intent=LegIntent.HIGHWAY_CONNECTOR,
                )
            )
        assert not route.called


class TestCapabilities:
    def test_does_not_claim_unpaved_preference(self, provider):
        assert provider.capabilities.prefers_unpaved is False

    def test_allows_faster_live_updates_than_a_metered_provider(self, provider):
        """Cheap per request, so the drag can refresh close to live."""
        assert provider.capabilities.live_update_interval_ms == 1000

    def test_declares_googles_waypoint_ceiling(self, provider):
        assert provider.capabilities.max_waypoints == 25
