"""Turning a corridor anchor into a name something can search for.

The stage that was missing. Brave cannot search "46.87,-121.52", so the spike used
hand-picked place names — which is why its search geography and its corridor geography
disagreed, and why places 100 km off the route kept arriving and being correctly discarded.

Naming the anchor closes the loop: the query is built from the same coordinate the distance
filter later measures against, so the two agree by construction rather than because someone
chose names that happened to be near the route. It also supplies the region qualifier that
stops "Cayuse" matching Oregon.

Reverse geocoding rather than a Places lookup: the question is "what is this place called",
not "what businesses are here".
"""

import logging
from typing import Any

import httpx

from motorooter.planning.discovery.errors import (
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.routing.models import COORDINATE_KEY_PRECISION, Coordinate

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

_NAME_TYPES = (
    "natural_feature",
    "park",
    "route",
    "locality",
    "neighborhood",
    "administrative_area_level_3",
)
"""Component types worth searching for, best first.

`route` sits above `locality` because of what a rural coordinate actually returns. Reverse
geocoding a point on Chinook Pass gives `Mather Memorial Parkway` as the road and `Enumclaw`
as the locality — and Enumclaw is fifty kilometres away over a mountain. Anchoring searches
on it produced leads around the wrong town, which the distance filter then correctly threw
away. The road at the coordinate is both nearer and a better search term: "viewpoint on
Mather Memorial Parkway" is the query a rider would type.

A county is never useful — "camping near Yakima County" returns a different and much worse
set of pages than "camping near Chinook Pass".
"""

_REGION_TYPE = "administrative_area_level_1"


class PlaceNamer:
    """Names anchors, caching each lookup for the life of the instance."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = GEOCODE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client = client
        self._timeout_s = timeout_s
        self._cache: dict[tuple[float, float], dict[str, Any] | None] = {}

    async def name_for(self, anchor: Coordinate) -> str | None:
        """A searchable name for this place, or `None` if there is not one.

        `None` rather than a coordinate: a coordinate in a web query matches nothing and
        costs a metered search to discover that.
        """
        found = await self._lookup(anchor)
        if found is None:
            return None

        components = found.get("address_components")
        if isinstance(components, list):
            for wanted in _NAME_TYPES:
                name = _component(components, wanted)
                if name:
                    return name

        # Better a rough name than no search at all for that stretch of route.
        formatted = found.get("formatted_address")
        return formatted.split(",")[0].strip() if isinstance(formatted, str) else None

    async def region_for(self, anchor: Coordinate) -> str | None:
        """The state or province, for disambiguating names that repeat."""
        found = await self._lookup(anchor)
        if found is None:
            return None
        components = found.get("address_components")
        return _component(components, _REGION_TYPE) if isinstance(components, list) else None

    async def _lookup(self, anchor: Coordinate) -> dict[str, Any] | None:
        """One request answers both questions, and each anchor is asked once.

        Keyed at the same precision the routing cache uses, so two points a metre apart are
        one place — which they are.
        """
        key = (
            round(anchor.lat, COORDINATE_KEY_PRECISION),
            round(anchor.lon, COORDINATE_KEY_PRECISION),
        )
        if key in self._cache:
            return self._cache[key]

        response = await self._get({"latlng": f"{anchor.lat},{anchor.lon}", "key": self._api_key})
        self._raise_for_status(response)
        found = self._first_result(response)
        self._cache[key] = found
        return found

    async def _get(self, params: dict[str, str]) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.get(
                    self._base_url, params=params, timeout=self._timeout_s
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.get(self._base_url, params=params)
        except httpx.HTTPError as exc:
            # The key is a query parameter on this API, so the message is built from the
            # exception type rather than interpolating anything that saw the URL.
            msg = f"reverse geocoding failed: {type(exc).__name__}"
            raise DiscoveryUnavailable(msg) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise DiscoveryRateLimited("Geocoding rate limit reached (HTTP 429)")
        if response.status_code >= 500:
            raise DiscoveryUnavailable(f"Geocoding returned HTTP {response.status_code}")
        if not response.is_success:
            raise DiscoveryRefused(f"Geocoding refused the request (HTTP {response.status_code})")

        try:
            status = response.json().get("status")
        except (ValueError, AttributeError):
            return
        if status in ("REQUEST_DENIED", "INVALID_REQUEST"):
            # Deliberately does not echo the body: this API takes its key in the query
            # string, and error payloads have been known to quote the request.
            raise DiscoveryRefused(f"Geocoding refused the request ({status})")
        if status == "OVER_QUERY_LIMIT":
            raise DiscoveryQuotaExceeded("Geocoding quota exhausted")

    @staticmethod
    def _first_result(response: httpx.Response) -> dict[str, Any] | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        results = body.get("results")
        if not isinstance(results, list) or not results:
            return None
        return results[0] if isinstance(results[0], dict) else None


def _component(components: list[Any], wanted: str) -> str | None:
    for entry in components:
        if not isinstance(entry, dict):
            continue
        types = entry.get("types")
        name = entry.get("long_name")
        if isinstance(types, list) and wanted in types and isinstance(name, str) and name:
            return name
    return None
