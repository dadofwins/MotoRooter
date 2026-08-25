"""In-memory routing provider.

Ships in `src` rather than `tests` because it is a supported seam: the test suite, local
development without API keys, and decorator tests all depend on it. It generates
deterministic interpolated geometry and records calls so decorator behaviour (caching,
retry, quota) can be asserted precisely.
"""

from itertools import pairwise

from motorooter.routing.errors import InvalidRequest, RoutingError
from motorooter.routing.geo import path_length_m
from motorooter.routing.models import (
    Coordinate,
    ProviderCapabilities,
    RouteLeg,
    RouteRequest,
    SurfaceSpan,
)

DEFAULT_CAPABILITIES = ProviderCapabilities(
    name="fake",
    prefers_unpaved=True,
    map_matching=True,
    alternatives=True,
    elevation=True,
    max_waypoints=50,
    live_update_interval_ms=0,  # never throttle in tests
)

_ASSUMED_SPEED_MS = 15.0
"""~54 km/h, a plausible mixed-surface average. Only needs to be positive and stable."""


class FakeProvider:
    """Deterministic provider that interpolates straight lines between waypoints."""

    def __init__(
        self,
        *,
        capabilities: ProviderCapabilities = DEFAULT_CAPABILITIES,
        points_per_segment: int = 8,
        surface_spans: tuple[SurfaceSpan, ...] = (),
        error: RoutingError | None = None,
        fail_first: int | None = None,
    ) -> None:
        """
        Args:
            capabilities: what this fake claims to support.
            points_per_segment: interpolated points emitted between consecutive waypoints.
            surface_spans: spans attached to every returned leg; must fit the geometry.
            error: if set, raised instead of routing.
            fail_first: raise `error` only for the first N calls, then succeed. `None`
                means fail every call. Lets retry tests assert recovery, not just failure.
        """
        if points_per_segment < 1:
            msg = "points_per_segment must be at least 1"
            raise ValueError(msg)
        self._capabilities = capabilities
        self._points_per_segment = points_per_segment
        self._surface_spans = surface_spans
        self._error = error
        self._fail_first = fail_first
        self.calls: list[RouteRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def call_count(self) -> int:
        """Calls that reached dispatch, including failed ones — quota is spent either way."""
        return len(self.calls)

    async def route(self, request: RouteRequest) -> RouteLeg:
        # Validate before recording: a request rejected here never hit the wire.
        if len(request.waypoints) > self._capabilities.max_waypoints:
            msg = (
                f"{len(request.waypoints)} waypoints exceeds provider maximum "
                f"{self._capabilities.max_waypoints}"
            )
            raise InvalidRequest(msg, provider=self._capabilities.name)

        self.calls.append(request)

        if self._error is not None and (
            self._fail_first is None or self.call_count <= self._fail_first
        ):
            raise self._error

        geometry = self._interpolate(request.waypoints)
        distance_m = path_length_m(geometry)
        return RouteLeg(
            geometry=geometry,
            distance_m=distance_m,
            duration_s=distance_m / _ASSUMED_SPEED_MS,
            surface_spans=self._surface_spans,
            provider=self._capabilities.name,
            intent=request.intent,
        )

    def _interpolate(self, waypoints: tuple[Coordinate, ...]) -> tuple[Coordinate, ...]:
        """Emit each waypoint exactly, with evenly spaced points between them."""
        points: list[Coordinate] = []
        for start, end in pairwise(waypoints):
            for step in range(self._points_per_segment):
                t = step / self._points_per_segment
                points.append(
                    Coordinate(
                        lat=start.lat + (end.lat - start.lat) * t,
                        lon=start.lon + (end.lon - start.lon) * t,
                    )
                )
        points.append(waypoints[-1])
        return tuple(points)
