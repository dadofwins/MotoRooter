"""Google Directions adapter, for on-road legs.

Cheap and high quality on pavement, which is why it handles highway connectors and twisty
paved sections while ORS takes the dirt. It exposes no surface information, so legs it
produces carry no surface spans — claiming otherwise would corrupt the unpaved statistics
the trip is judged on.

Note the API signals failure in the response body with HTTP 200, so `status` must be
inspected on every successful-looking response.
"""

from typing import Any

import httpx

from motorooter.routing.errors import (
    InvalidRequest,
    NoRouteFound,
    ProviderUnavailable,
    QuotaExceeded,
    RoutingError,
)
from motorooter.routing.models import (
    Coordinate,
    ProviderCapabilities,
    RouteLeg,
    RouteRequest,
)
from motorooter.routing.providers.polyline import decode_polyline

GOOGLE_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"

_STATUS_ERRORS: dict[str, type[RoutingError]] = {
    "ZERO_RESULTS": NoRouteFound,
    "NOT_FOUND": InvalidRequest,
    "INVALID_REQUEST": InvalidRequest,
    "MAX_WAYPOINTS_EXCEEDED": InvalidRequest,
    "MAX_ROUTE_LENGTH_EXCEEDED": InvalidRequest,
    "REQUEST_DENIED": InvalidRequest,
    "OVER_QUERY_LIMIT": QuotaExceeded,
    "OVER_DAILY_LIMIT": QuotaExceeded,
    "UNKNOWN_ERROR": ProviderUnavailable,  # documented as transient; retryable
}

CAPABILITIES = ProviderCapabilities(
    name="google",
    prefers_unpaved=False,
    # A car profile, so its time is the one to show. Measured against our speed table on
    # 177 km of highway: Google 39 min, derived 57 min. Ours overestimates on tarmac.
    reports_trustworthy_duration=True,
    map_matching=False,
    alternatives=True,
    elevation=False,
    max_waypoints=25,
    live_update_interval_ms=1000,
    daily_quota=None,
)


class GoogleDirectionsProvider:
    """Routes on-road legs via the Google Directions API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = GOOGLE_DIRECTIONS_URL,
        client: httpx.AsyncClient | None = None,
        capabilities: ProviderCapabilities = CAPABILITIES,
        timeout_s: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client = client
        self._capabilities = capabilities
        self._timeout_s = timeout_s

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def route(self, request: RouteRequest) -> RouteLeg:
        if len(request.waypoints) > self._capabilities.max_waypoints:
            msg = (
                f"{len(request.waypoints)} waypoints exceeds Google maximum "
                f"{self._capabilities.max_waypoints}"
            )
            raise InvalidRequest(msg, provider=self._capabilities.name)

        response = await self._get(self._build_params(request))
        if response.status_code >= 500:
            msg = f"upstream returned {response.status_code}"
            raise ProviderUnavailable(msg, provider=self._capabilities.name)

        body = self._json(response)
        self._raise_for_api_status(body)
        return self._parse(body, request)

    def _build_params(self, request: RouteRequest) -> dict[str, str]:
        origin, *middle, destination = request.waypoints
        params = {
            "origin": _latlng(origin),
            "destination": _latlng(destination),
            "mode": "driving",
            "key": self._api_key,
        }
        if middle:
            # `via:` shapes the route without inserting stopovers, which would split legs.
            params["waypoints"] = "|".join(f"via:{_latlng(wp)}" for wp in middle)
        avoid = [
            feature
            for feature, enabled in (
                ("highways", request.avoid_highways),
                ("tolls", request.avoid_tolls),
                ("ferries", request.avoid_ferries),
            )
            if enabled
        ]
        if avoid:
            params["avoid"] = "|".join(avoid)
        return params

    async def _get(self, params: dict[str, str]) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.get(
                    self._base_url, params=params, timeout=self._timeout_s
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.get(self._base_url, params=params)
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise ProviderUnavailable(msg, provider=self._capabilities.name) from exc

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            msg = "response was not JSON"
            raise ProviderUnavailable(msg, provider=self._capabilities.name) from exc
        if not isinstance(body, dict):
            msg = "unrecognized Google response shape"
            raise ProviderUnavailable(msg, provider=self._capabilities.name)
        return body

    def _raise_for_api_status(self, body: dict[str, Any]) -> None:
        status = body.get("status", "UNKNOWN_ERROR")
        if status == "OK":
            return
        message = body.get("error_message", status)
        error_cls = _STATUS_ERRORS.get(status, ProviderUnavailable)
        raise error_cls(message, provider=self._capabilities.name)

    def _parse(self, body: dict[str, Any], request: RouteRequest) -> RouteLeg:
        name = self._capabilities.name
        routes = body.get("routes") or []
        if not routes:
            msg = "Google returned no route between the requested waypoints"
            raise NoRouteFound(msg, provider=name)

        try:
            legs = routes[0]["legs"]
            geometry: list[Coordinate] = []
            distance_m = 0.0
            duration_s = 0.0
            for leg in legs:
                distance_m += leg["distance"]["value"]
                duration_s += leg["duration"]["value"]
                for step in leg["steps"]:
                    # Step polylines, not overview_polyline: the overview is decimated,
                    # and GPX export and drag splicing both need full detail.
                    points = decode_polyline(step["polyline"]["points"])
                    if geometry and points and geometry[-1] == points[0]:
                        points = points[1:]  # steps share their boundary point
                    geometry.extend(points)
        except (TypeError, KeyError, IndexError, ValueError) as exc:
            # The exception itself goes to the operator on `__cause__`, never into the
            # message: `ToolCallFailed` forwards this text to the rail, and a rider once
            # read a pydantic validation report complete with its documentation URL.
            msg = "could not parse the Google response"
            raise ProviderUnavailable(msg, provider=name) from exc

        if len(geometry) < 2:
            msg = "Google returned a degenerate route"
            raise NoRouteFound(msg, provider=name)

        return RouteLeg(
            geometry=tuple(geometry),
            distance_m=distance_m,
            duration_s=duration_s,
            surface_spans=(),  # Google exposes no surface tags
            ascent_m=None,
            provider=name,
            intent=request.intent,
        )


def _latlng(point: Coordinate) -> str:
    """Google's parameter order is lat,lng — the opposite of GeoJSON."""
    return f"{point.lat},{point.lon}"
