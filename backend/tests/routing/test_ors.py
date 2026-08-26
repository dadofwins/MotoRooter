"""OpenRouteService adapter, driven entirely by recorded fixtures. Never hits the network."""

from typing import Any

import httpx
import pytest
import respx

from motorooter.routing.errors import (
    InvalidRequest,
    NoRouteFound,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from motorooter.routing.models import Coordinate, LegIntent, RouteRequest, Surface
from motorooter.routing.providers.ors import (
    ORS_BASE_URL,
    ORS_DEFAULT_SNAP_RADIUS_M,
    OrsProvider,
    ascent_from,
)
from tests.routing.contract import RoutingProviderContract

DIRECTIONS_URL = respx.patterns.M(url__startswith=f"{ORS_BASE_URL}/v2/directions/")


def ors_geojson(
    coordinates: list[list[float]],
    *,
    distance: float = 95_000.0,
    duration: float = 5_400.0,
    surface_values: list[list[int]] | None = None,
    ascent: float | None = None,
) -> dict[str, Any]:
    """Minimal ORS GeoJSON response shaped like the real one."""
    properties: dict[str, Any] = {"summary": {"distance": distance, "duration": duration}}
    if surface_values is not None:
        properties["extras"] = {"surface": {"values": surface_values}}
    if ascent is not None:
        properties["ascent"] = ascent
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": properties,
            }
        ],
    }


def echo_requested_coordinates(request: httpx.Request) -> httpx.Response:
    """Respond with geometry running through exactly the requested waypoints."""
    import json

    coords = json.loads(request.content)["coordinates"]
    return httpx.Response(200, json=ors_geojson(coords))


@pytest.fixture
def mock_ors():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def provider():
    return OrsProvider(api_key="test-key")


@pytest.fixture
def pdx_hood():
    return RouteRequest(
        waypoints=(
            Coordinate(lat=45.5152, lon=-122.6784),
            Coordinate(lat=45.3311, lon=-121.7113),
        ),
        intent=LegIntent.UNPAVED,
    )


class TestOrsContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self, mock_ors):
        mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        return OrsProvider(api_key="test-key")


class TestRequestConstruction:
    async def test_sends_coordinates_in_geojson_lon_lat_order(self, provider, mock_ors, pdx_hood):
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(pdx_hood)
        body = route.calls.last.request.content
        import json

        assert json.loads(body)["coordinates"][0] == [-122.6784, 45.5152]

    async def test_authenticates_with_the_api_key(self, provider, mock_ors, pdx_hood):
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(pdx_hood)
        assert route.calls.last.request.headers["authorization"] == "test-key"

    async def test_always_requests_surface_extras(self, provider, mock_ors, pdx_hood):
        """Surface data is the whole point of routing dirt legs through ORS."""
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(pdx_hood)
        import json

        assert "surface" in json.loads(route.calls.last.request.content)["extra_info"]

    async def test_intent_selects_the_profile(self, provider, mock_ors):
        """Per-road-type algorithm choice, expressed as a profile in the URL path."""
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        for intent, expected in [
            (LegIntent.HIGHWAY_CONNECTOR, "driving-car"),
            (LegIntent.TECHNICAL_OFFROAD, "cycling-mountain"),
        ]:
            await provider.route(
                RouteRequest(
                    waypoints=(
                        Coordinate(lat=45.0, lon=-121.0),
                        Coordinate(lat=46.0, lon=-121.0),
                    ),
                    intent=intent,
                )
            )
            assert expected in str(route.calls.last.request.url)

    async def test_profile_map_is_configurable(self, mock_ors):
        provider = OrsProvider(api_key="k", profile_for_intent={LegIntent.UNPAVED: "driving-hgv"})
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(
            RouteRequest(
                waypoints=(Coordinate(lat=45.0, lon=-121.0), Coordinate(lat=46.0, lon=-121.0)),
                intent=LegIntent.UNPAVED,
            )
        )
        assert "driving-hgv" in str(route.calls.last.request.url)

    async def test_elevation_requested_only_when_asked(self, provider, mock_ors):
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await provider.route(
            RouteRequest(
                waypoints=(Coordinate(lat=45.0, lon=-121.0), Coordinate(lat=46.0, lon=-121.0)),
                intent=LegIntent.UNPAVED,
                want_elevation=True,
            )
        )
        import json

        assert json.loads(route.calls.last.request.content)["elevation"] is True


class TestResponseParsing:
    async def test_parses_geometry_into_coordinates(self, provider, mock_ors, pdx_hood):
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson([[-122.6784, 45.5152], [-122.0, 45.4], [-121.7113, 45.3311]])
        )
        leg = await provider.route(pdx_hood)
        assert leg.geometry[1] == Coordinate(lat=45.4, lon=-122.0)

    async def test_ignores_third_elevation_ordinate(self, provider, mock_ors, pdx_hood):
        """Elevation-enabled responses carry [lon, lat, ele] positions."""
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson([[-122.6784, 45.5152, 15.0], [-121.7113, 45.3311, 1200.0]])
        )
        leg = await provider.route(pdx_hood)
        assert leg.geometry[0] == Coordinate(lat=45.5152, lon=-122.6784)

    async def test_parses_distance_and_duration(self, provider, mock_ors, pdx_hood):
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson(
                [[-122.6784, 45.5152], [-121.7113, 45.3311]], distance=1234.0, duration=567.0
            )
        )
        leg = await provider.route(pdx_hood)
        assert (leg.distance_m, leg.duration_s) == (1234.0, 567.0)

    async def test_ascent_comes_from_the_elevations_not_the_property(
        self, provider, mock_ors, pdx_hood
    ):
        """`properties.ascent` is no longer read. It is self-consistent with the elevations
        ORS returns and both are wrong together when a lookup fails, so the number is
        computed from elevations we have filtered — see `TestElevationSentinels`."""
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson(
                [[-122.6784, 45.5152, 100.0], [-121.7113, 45.3311, 1600.0]], ascent=9999.0
            )
        )
        leg = await provider.route(pdx_hood)
        assert leg.ascent_m == pytest.approx(1500.0)

    async def test_ascent_is_none_when_absent(self, provider, mock_ors, pdx_hood):
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson([[-122.6784, 45.5152], [-121.7113, 45.3311]])
        )
        assert (await provider.route(pdx_hood)).ascent_m is None

    async def test_tags_the_leg_as_ors(self, provider, mock_ors, pdx_hood):
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson([[-122.6784, 45.5152], [-121.7113, 45.3311]])
        )
        assert (await provider.route(pdx_hood)).provider == "ors"

    async def test_empty_feature_list_is_no_route_found(self, provider, mock_ors, pdx_hood):
        mock_ors.route(DIRECTIONS_URL).respond(json={"type": "FeatureCollection", "features": []})
        with pytest.raises(NoRouteFound):
            await provider.route(pdx_hood)

    async def test_malformed_payload_is_a_routing_error(self, provider, mock_ors, pdx_hood):
        """A shape change upstream must not surface as a raw KeyError."""
        mock_ors.route(DIRECTIONS_URL).respond(json={"unexpected": True})
        with pytest.raises(ProviderUnavailable):
            await provider.route(pdx_hood)


class TestSurfaceMapping:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (0, Surface.UNKNOWN),
            (1, Surface.PAVED),
            (2, Surface.UNPAVED),
            (3, Surface.PAVED),  # asphalt
            (10, Surface.UNPAVED),  # gravel
            (11, Surface.UNPAVED),  # dirt
            (14, Surface.PAVED),  # paving stones
            (17, Surface.UNPAVED),  # grass
        ],
    )
    async def test_maps_ors_surface_codes(self, provider, mock_ors, pdx_hood, code, expected):
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson(
                [[-122.6784, 45.5152], [-122.0, 45.4], [-121.7113, 45.3311]],
                surface_values=[[0, 2, code]],
            )
        )
        leg = await provider.route(pdx_hood)
        assert leg.surface_spans[0].surface is expected

    async def test_unrecognized_code_is_unknown_not_unpaved(self, provider, mock_ors, pdx_hood):
        """A new upstream code must not be silently counted as dirt."""
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson(
                [[-122.6784, 45.5152], [-121.7113, 45.3311]], surface_values=[[0, 1, 999]]
            )
        )
        leg = await provider.route(pdx_hood)
        assert leg.surface_spans[0].surface is Surface.UNKNOWN

    async def test_drops_zero_length_spans(self, provider, mock_ors, pdx_hood):
        """ORS occasionally emits start == end; the model rejects those."""
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson(
                [[-122.6784, 45.5152], [-122.0, 45.4], [-121.7113, 45.3311]],
                surface_values=[[0, 0, 11], [0, 2, 11]],
            )
        )
        leg = await provider.route(pdx_hood)
        assert len(leg.surface_spans) == 1

    async def test_computes_unpaved_fraction(self, provider, mock_ors, pdx_hood):
        mock_ors.route(DIRECTIONS_URL).respond(
            json=ors_geojson(
                [[-121.0, 45.0], [-121.0, 46.0], [-121.0, 47.0]],
                surface_values=[[0, 1, 3], [1, 2, 11]],
            )
        )
        leg = await provider.route(pdx_hood)
        assert leg.unpaved_fraction == pytest.approx(0.5, rel=0.01)


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "code", "expected"),
        [
            (404, 2009, NoRouteFound),
            (400, 2010, InvalidRequest),
            (400, 2004, InvalidRequest),
            # 429 is the per-minute ceiling and clears in seconds; 403 is the daily
            # budget being gone. Same family, opposite advice to the caller.
            (429, None, RateLimited),
            (403, None, QuotaExceeded),
            (500, None, ProviderUnavailable),
            (503, None, ProviderUnavailable),
        ],
    )
    async def test_maps_upstream_failures(
        self, provider, mock_ors, pdx_hood, status, code, expected
    ):
        body = {"error": {"code": code, "message": "upstream said no"}} if code else {}
        mock_ors.route(DIRECTIONS_URL).respond(status_code=status, json=body)
        with pytest.raises(expected):
            await provider.route(pdx_hood)

    async def test_timeout_is_retryable(self, provider, mock_ors, pdx_hood):
        mock_ors.route(DIRECTIONS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        with pytest.raises(ProviderUnavailable) as exc:
            await provider.route(pdx_hood)
        assert exc.value.retryable is True

    async def test_too_many_waypoints_rejected_before_dispatch(self, provider, mock_ors):
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        n = provider.capabilities.max_waypoints + 1
        with pytest.raises(InvalidRequest):
            await provider.route(
                RouteRequest(
                    waypoints=tuple(Coordinate(lat=45.0 + i * 0.01, lon=-121.0) for i in range(n)),
                    intent=LegIntent.UNPAVED,
                )
            )
        assert not route.called


class TestCapabilities:
    def test_declares_unpaved_preference(self, provider):
        assert provider.capabilities.prefers_unpaved is True

    def test_declares_the_free_tier_quota(self, provider):
        assert provider.capabilities.daily_quota == 2000

    def test_refreshes_about_once_a_second_during_a_drag(self, provider):
        """This was 3000 ms, to protect the free-tier budget during a drag.

        That reasoning expired twice: cost is not a constraint on this prototype, and the
        per-minute rate limiter now guards bursts far better than a blunt throttle. Measured,
        ORS answers in ~1.25 s whatever the route size, so 3000 ms made an update take about
        five seconds — which a rider noticed before anyone re-examined the number.
        """
        assert provider.capabilities.live_update_interval_ms == 1000


class TestSnapRadius:
    """How far ORS may look for a routable way near a requested point.

    ORS defaults to 350 m, which is tuned for dense urban networks and is wrong for mountain
    terrain — especially on `cycling-mountain`, whose routable network is far sparser than a
    car's. Measured against the live API, 44% of plausible map clicks across the Cascades
    failed to snap at the default: no bike-legal way within 350 m of a national park pass or
    a freeway-adjacent point. A click that returns 400 is worse than a route that starts a
    little way off.
    """

    async def test_a_snap_radius_is_sent(self, mock_ors):
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await OrsProvider(api_key="k").route(
            RouteRequest(
                waypoints=(
                    Coordinate(lat=45.5, lon=-122.6),
                    Coordinate(lat=45.3, lon=-121.7),
                ),
                intent=LegIntent.UNPAVED,
            )
        )
        import json as _json

        payload = _json.loads(route.calls.last.request.content)
        assert payload["radiuses"] == [ORS_DEFAULT_SNAP_RADIUS_M, ORS_DEFAULT_SNAP_RADIUS_M]

    async def test_the_default_is_wide_enough_for_mountain_terrain(self):
        """350 m is the ORS default and the thing being overridden."""
        assert ORS_DEFAULT_SNAP_RADIUS_M >= 1000.0

    async def test_there_is_one_radius_per_waypoint(self, mock_ors):
        """ORS matches radiuses to coordinates positionally; a short list is rejected."""
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        waypoints = tuple(Coordinate(lat=45.0 + index * 0.1, lon=-121.0) for index in range(4))
        await OrsProvider(api_key="k").route(
            RouteRequest(waypoints=waypoints, intent=LegIntent.UNPAVED)
        )
        import json as _json

        payload = _json.loads(route.calls.last.request.content)
        assert len(payload["radiuses"]) == len(waypoints)

    async def test_it_is_configurable_rather_than_a_constant(self, mock_ors):
        """It is a guess, like the gap threshold and the twistiness segment. Tunable
        without editing the adapter."""
        route = mock_ors.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
        await OrsProvider(api_key="k", snap_radius_m=250.0).route(
            RouteRequest(
                waypoints=(
                    Coordinate(lat=45.5, lon=-122.6),
                    Coordinate(lat=45.3, lon=-121.7),
                ),
                intent=LegIntent.UNPAVED,
            )
        )
        import json as _json

        assert _json.loads(route.calls.last.request.content)["radiuses"] == [250.0, 250.0]

    async def test_a_non_positive_radius_is_refused(self):
        """Zero would snap nothing; negative means "unlimited" to ORS, which is not a
        default anyone should get by typo."""
        with pytest.raises(ValueError):
            OrsProvider(api_key="k", snap_radius_m=0.0)


class TestElevationSentinels:
    """ORS returns 0 when its elevation lookup fails, and the ascent it reports believes it.

    Measured on two live corridors. `cycling-mountain` returned twelve zero-elevation points
    out of 2,763 on Ellensburg-Cashmere and four of 311 on Chinook Pass; `driving-car`
    returned none on either. Each excursion to sea level and back adds twice the local
    elevation to a cumulative sum, so twelve points inflated the reported ascent from 3,605 m
    to 6,729 m — and 229 m/km over a paved highway pass, which is a sustained 23% gradient.

    ORS is self-consistent about it: its reported `ascent` equals a naive sum over its own
    elevations to the metre. The data is what is wrong, so the fix is to stop believing that
    one field and compute ascent from elevations we have filtered.

    **Zero is only a sentinel when it is implausible.** Sea level is a real elevation, and a
    coastal route must not have its geometry treated as corrupt — so what marks a sentinel is
    the discontinuity, not the value.
    """

    @staticmethod
    def _positions(elevations: list[float]) -> list[list[float]]:
        return [[-121.0 + i * 0.001, 47.0, e] for i, e in enumerate(elevations)]

    def test_a_clean_climb_is_summed_naively(self):
        """Same method as the reference figure: positive deltas, no smoothing, no threshold."""
        assert ascent_from(self._positions([100.0, 150.0, 120.0, 200.0])) == pytest.approx(130.0)

    def test_a_sentinel_zero_is_ignored(self):
        """The 1,200 m plunge and recovery is a failed lookup, not a cliff."""
        clean = self._positions([1200.0, 1210.0, 1220.0])
        with_sentinel = self._positions([1200.0, 0.0, 1210.0, 1220.0])
        assert ascent_from(with_sentinel) == pytest.approx(ascent_from(clean))

    def test_several_sentinels_are_ignored(self):
        assert ascent_from(self._positions([1200.0, 0.0, 1210.0, 0.0, 1220.0])) == pytest.approx(
            20.0
        )

    def test_sea_level_is_not_a_sentinel(self):
        """A coastal route legitimately touches zero, and its geometry is not corrupt."""
        assert ascent_from(self._positions([0.0, 5.0, 0.0, 10.0])) == pytest.approx(15.0)

    def test_a_route_that_starts_at_zero_is_kept(self):
        assert ascent_from(self._positions([0.0, 1.0, 2.0])) == pytest.approx(2.0)

    def test_a_leading_sentinel_is_ignored(self):
        """Nothing before it to compare against, so the judgement is against what follows."""
        assert ascent_from(self._positions([0.0, 1500.0, 1510.0])) == pytest.approx(10.0)

    def test_positions_without_elevation_give_nothing(self):
        assert ascent_from([[-121.0, 47.0], [-120.9, 47.0]]) is None

    def test_an_empty_route_gives_nothing(self):
        assert ascent_from([]) is None


class TestAscentIsComputedNotTrusted:
    async def test_the_reported_ascent_is_not_used_when_it_disagrees(
        self, mock_ors, provider, pdx_hood
    ):
        """ORS reports its own inflated figure. We compute from the elevations instead."""
        body = ors_geojson(
            coordinates=[[-121.0, 47.0, 1200.0], [-121.0, 47.001, 0.0], [-121.0, 47.002, 1210.0]],
            ascent=2410.0,
        )
        mock_ors.route(DIRECTIONS_URL).respond(json=body)
        leg = await provider.route(pdx_hood)
        assert leg.ascent_m == pytest.approx(10.0)

    async def test_no_elevation_means_no_ascent(self, mock_ors, provider, pdx_hood):
        """Elevation is opt-in; a leg routed without it should not claim a climb of zero."""
        body = ors_geojson(coordinates=[[-121.0, 47.0], [-121.0, 47.001]])
        mock_ors.route(DIRECTIONS_URL).respond(json=body)
        assert (await provider.route(pdx_hood)).ascent_m is None
