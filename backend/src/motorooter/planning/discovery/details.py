"""Display data for the POI dialog, fetched fresh every time.

Separate from `resolve` because the constraint is opposite. Resolution asks for the minimum
and keeps the `place_id` forever; this asks for everything a dialog shows and keeps none of
it. Google's terms permit storing `place_id` indefinitely and very little else, so ratings,
photos, hours and reviews live only for the length of a response.

Which means: **no server-side cache here, deliberately.** The frontend asserts the behaviour
by reopening the dialog and expecting a second request, and a cache would defeat both their
test and the terms it encodes.

Absence is ordinary. Dispersed camping is the thing this app cares most about and the thing
Places knows least about — no rating, no photo, no hours. That is a normal response.
"""

from typing import Any

import httpx

from motorooter.planning.discovery.errors import (
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.routing.models import Coordinate

PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places"

DETAIL_FIELD_MASK = (
    "id,displayName,location,types,rating,userRatingCount,"
    "websiteUri,nationalPhoneNumber,regularOpeningHours.weekdayDescriptions,reviews.text"
)
"""Everything the dialog shows, and nothing else.

Wider than the resolve mask on purpose: this is the one place the extra fields are actually
displayed, so this is the one place worth paying the higher tier for. Photos are omitted
until something renders them — Places photos need a second request per image to turn a
reference into bytes, which is a cost with no current benefit.
"""


class PlaceDetails:
    """One place, as Places currently describes it."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = PLACE_DETAILS_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s

    async def fetch(self, place_id: str) -> dict[str, Any]:
        """Raw detail for a place.

        Raises:
            DiscoveryRefused: unknown id, or a key that cannot make server-side calls.
            DiscoveryRateLimited / DiscoveryQuotaExceeded / DiscoveryUnavailable: as usual.
        """
        response = await self._get(place_id)
        self._raise_for_status(response, place_id)
        try:
            body = response.json()
        except ValueError as exc:
            msg = "Places returned a body that was not JSON"
            raise DiscoveryUnavailable(msg) from exc
        if not isinstance(body, dict):
            msg = "unrecognized Places detail response"
            raise DiscoveryUnavailable(msg)
        return body

    async def _get(self, place_id: str) -> httpx.Response:
        headers = {
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": DETAIL_FIELD_MASK,
        }
        try:
            url = f"{self._base_url}/{place_id}"
            if self._client is not None:
                return await self._client.get(url, headers=headers, timeout=self._timeout_s)
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"Places detail request failed: {exc}"
            raise DiscoveryUnavailable(msg) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response, place_id: str) -> None:
        if response.is_success:
            return
        status = response.status_code
        if status == 404:
            raise DiscoveryRefused(f"no place with id {place_id!r}")
        if status == 403:
            # Same trap as resolve: the server key is currently the browser key, and a
            # referrer restriction would make every server-side call fail like this.
            msg = (
                "Places refused the request (HTTP 403). If the key has been restricted by "
                "HTTP referrer, server-side calls cannot satisfy it — they send no referrer."
            )
            raise DiscoveryRefused(msg)
        if status == 429:
            raise DiscoveryRateLimited("Places rate limit reached (HTTP 429)")
        if status == 402:
            raise DiscoveryQuotaExceeded("Places billing quota exhausted (HTTP 402)")
        if status >= 500:
            raise DiscoveryUnavailable(f"Places returned HTTP {status}")
        raise DiscoveryRefused(f"Places rejected the request (HTTP {status})")


def coordinate_in(body: dict[str, Any]) -> Coordinate | None:
    """The location, if Places gave a usable one."""
    location = body.get("location")
    if not isinstance(location, dict):
        return None
    lat, lon = location.get("latitude"), location.get("longitude")
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    try:
        return Coordinate(lat=float(lat), lon=float(lon))
    except ValueError:
        return None


def strings_in(value: Any) -> tuple[str, ...]:  # noqa: ANN401 -- raw upstream JSON
    """Non-empty strings from a list, tolerating anything else in it."""
    if not isinstance(value, list):
        return ()
    return tuple(entry.strip() for entry in value if isinstance(entry, str) and entry.strip())
