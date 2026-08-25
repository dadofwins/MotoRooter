"""OpenRouteService adapter.

Chosen over GraphHopper because GraphHopper's free plan disables flexible mode — the
custom-model mechanism for weighting surface and track type — which is precisely the
feature adventure routing depends on. ORS is also self-hostable behind an identical API,
so outgrowing the free tier costs infrastructure rather than a rewrite.

Known limitation: hosted ORS has no motorcycle profile. Dirt-capable intents are routed
through `cycling-mountain`, which reaches tracks a car profile refuses but applies
bicycle access rules. That approximation is the main reason to move to a self-hosted
instance with a custom moto profile.
"""

from collections.abc import Mapping
from typing import Any

import httpx

from motorooter.routing.errors import (
    InvalidRequest,
    NoRouteFound,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    ProviderCapabilities,
    RouteLeg,
    RouteRequest,
    Surface,
    SurfaceSpan,
)

ORS_BASE_URL = "https://api.openrouteservice.org"

ORS_DEFAULT_SNAP_RADIUS_M = 5000.0
"""How far ORS may look for a routable way near a requested point.

ORS defaults to 350 m, which suits dense urban networks and is badly wrong for mountain
terrain. It is worst on `cycling-mountain`, whose routable network is far sparser than a
car's: measured against the live API, 44% of plausible map clicks across the Cascades failed
to snap at the default, every one of them "could not find routable point within 350.0 m".
A click that returns 400 is a worse answer than a route that starts a little way off.

    350 m    2/5 points snapped
    1000 m   4/5
    5000 m   5/5

A guess like the gap threshold and the twistiness segment, so it is a constructor argument
as well as a constant.

The cost of a wide radius is that snapping becomes surprising: a rider who taps a specific
trailhead can get a route starting a kilometre away with nothing saying so. Failing outright
is still worse, but the displacement is worth surfacing — `planning.metrics.nearest_distance_m`
already computes it.
"""

DEFAULT_PROFILE_FOR_INTENT: Mapping[LegIntent, str] = {
    LegIntent.HIGHWAY_CONNECTOR: "driving-car",
    LegIntent.TWISTY_PAVED: "driving-car",
    LegIntent.UNPAVED: "cycling-mountain",
    LegIntent.TECHNICAL_OFFROAD: "cycling-mountain",
    LegIntent.MANUAL_TRACK: "driving-car",
}

_SURFACE_CODES: Mapping[int, Surface] = {
    0: Surface.UNKNOWN,
    1: Surface.PAVED,
    2: Surface.UNPAVED,
    3: Surface.PAVED,  # asphalt
    4: Surface.PAVED,  # concrete
    5: Surface.PAVED,  # cobblestone
    6: Surface.PAVED,  # metal
    7: Surface.PAVED,  # wood
    8: Surface.UNPAVED,  # compacted gravel
    9: Surface.UNPAVED,  # fine gravel
    10: Surface.UNPAVED,  # gravel
    11: Surface.UNPAVED,  # dirt
    12: Surface.UNPAVED,  # ground
    13: Surface.UNPAVED,  # ice
    14: Surface.PAVED,  # paving stones
    15: Surface.UNPAVED,  # sand
    16: Surface.UNPAVED,  # woodchips
    17: Surface.UNPAVED,  # grass
    18: Surface.UNPAVED,  # grass paver
}

_ROUTE_NOT_FOUND_CODES = frozenset({2009})

CAPABILITIES = ProviderCapabilities(
    name="ors",
    prefers_unpaved=True,
    reports_surface=True,  # via extra_info=surface
    map_matching=False,
    alternatives=True,
    elevation=True,
    max_waypoints=50,
    live_update_interval_ms=3000,
    daily_quota=2000,
    per_minute_quota=40,
)


class OrsProvider:
    """Routes via the hosted OpenRouteService Directions API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = ORS_BASE_URL,
        client: httpx.AsyncClient | None = None,
        profile_for_intent: Mapping[LegIntent, str] | None = None,
        capabilities: ProviderCapabilities = CAPABILITIES,
        snap_radius_m: float = ORS_DEFAULT_SNAP_RADIUS_M,
        timeout_s: float = 20.0,
    ) -> None:
        """
        Args:
            api_key: ORS API key.
            base_url: override to point at a self-hosted instance.
            client: injectable HTTP client, so callers can share a connection pool.
            profile_for_intent: intent -> ORS profile. Config, not code, so retuning which
                engine profile serves which road type never touches this module.
            capabilities: override when pointing at a self-hosted instance with different
                limits (a local instance has no daily quota).
            snap_radius_m: how far to look for a routable way near each waypoint. See
                `ORS_DEFAULT_SNAP_RADIUS_M` for why the ORS default is unusable here.
            timeout_s: per-request timeout.
        """
        if snap_radius_m <= 0:
            # Zero snaps nothing; ORS reads a negative as "unlimited", which is not a
            # setting anyone should arrive at by typo.
            msg = f"snap_radius_m must be positive, got {snap_radius_m}"
            raise ValueError(msg)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._profiles = dict(profile_for_intent or DEFAULT_PROFILE_FOR_INTENT)
        self._capabilities = capabilities
        self._snap_radius_m = snap_radius_m
        self._timeout_s = timeout_s

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def route(self, request: RouteRequest) -> RouteLeg:
        if len(request.waypoints) > self._capabilities.max_waypoints:
            msg = (
                f"{len(request.waypoints)} waypoints exceeds ORS maximum "
                f"{self._capabilities.max_waypoints}"
            )
            raise InvalidRequest(msg, provider=self._capabilities.name)

        profile = self._profiles.get(request.intent)
        if profile is None:
            msg = f"no ORS profile configured for intent {request.intent.value!r}"
            raise InvalidRequest(msg, provider=self._capabilities.name)

        payload = self._build_payload(request)
        url = f"{self._base_url}/v2/directions/{profile}/geojson"
        headers = {"Authorization": self._api_key, "Content-Type": "application/json"}

        response = await self._post(url, payload, headers)
        self._raise_for_status(response)
        return self._parse(response.json(), request)

    def _build_payload(self, request: RouteRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "coordinates": [list(wp.to_geojson()) for wp in request.waypoints],
            "extra_info": ["surface", "waytype"],
            "elevation": request.want_elevation,
            # One radius per coordinate: ORS matches them positionally and rejects a list
            # of the wrong length.
            "radiuses": [self._snap_radius_m] * len(request.waypoints),
        }
        avoid = [
            feature
            for feature, enabled in (
                ("highways", request.avoid_highways),
                ("tollways", request.avoid_tolls),
                ("ferries", request.avoid_ferries),
            )
            if enabled
        ]
        if avoid:
            payload["options"] = {"avoid_features": avoid}
        return payload

    async def _post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout_s
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise ProviderUnavailable(msg, provider=self._capabilities.name) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return

        name = self._capabilities.name
        status = response.status_code

        if status >= 500:
            msg = f"upstream returned {status}"
            raise ProviderUnavailable(msg, provider=name)
        if status == 429:
            # The per-minute ceiling. Clears in under a minute, so this is retryable and
            # must not be reported as the daily budget being gone.
            msg = "per-minute rate limit reached (HTTP 429)"
            raise RateLimited(msg, provider=name)
        if status == 403:
            msg = "daily quota exhausted (HTTP 403)"
            raise QuotaExceeded(msg, provider=name)

        error = self._error_body(response)
        code = error.get("code")
        message = error.get("message", f"HTTP {status}")
        if code in _ROUTE_NOT_FOUND_CODES:
            raise NoRouteFound(message, provider=name)
        raise InvalidRequest(f"{message} (code {code})", provider=name)

    @staticmethod
    def _error_body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        error = body.get("error") if isinstance(body, dict) else None
        return error if isinstance(error, dict) else {}

    def _parse(self, body: Any, request: RouteRequest) -> RouteLeg:  # noqa: ANN401 -- raw JSON
        name = self._capabilities.name
        try:
            features = body["features"]
        except (TypeError, KeyError) as exc:
            msg = "unrecognized ORS response shape"
            raise ProviderUnavailable(msg, provider=name) from exc

        if not features:
            msg = "ORS returned no route between the requested waypoints"
            raise NoRouteFound(msg, provider=name)

        try:
            feature = features[0]
            positions = feature["geometry"]["coordinates"]
            properties = feature["properties"]
            summary = properties["summary"]
            # ORS positions are [lon, lat] or [lon, lat, elevation]; drop the third ordinate.
            geometry = tuple(Coordinate(lat=pos[1], lon=pos[0]) for pos in positions)
            return RouteLeg(
                geometry=geometry,
                distance_m=summary["distance"],
                duration_s=summary["duration"],
                surface_spans=self._parse_surface_spans(properties, len(geometry) - 1),
                ascent_m=properties.get("ascent"),
                provider=name,
                intent=request.intent,
            )
        except (TypeError, KeyError, IndexError, ValueError) as exc:
            msg = f"could not parse ORS response: {exc}"
            raise ProviderUnavailable(msg, provider=name) from exc

    @staticmethod
    def _parse_surface_spans(
        properties: Any,  # noqa: ANN401 -- raw upstream JSON
        max_index: int,
    ) -> tuple[SurfaceSpan, ...]:
        """Translate `extras.surface.values` triples into spans.

        Unrecognized codes become UNKNOWN rather than UNPAVED: a new upstream code must not
        silently inflate the dirt statistics the trip is judged on.
        """
        values = properties.get("extras", {}).get("surface", {}).get("values", [])
        spans = []
        for start, end, code in values:
            # ORS occasionally emits degenerate or over-long spans; clamp and drop them.
            end = min(end, max_index)
            if end <= start:
                continue
            spans.append(
                SurfaceSpan(
                    start_index=start,
                    end_index=end,
                    surface=_SURFACE_CODES.get(code, Surface.UNKNOWN),
                )
            )
        return tuple(spans)
