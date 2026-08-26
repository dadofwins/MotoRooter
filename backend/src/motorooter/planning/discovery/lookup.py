"""Turning a name into places that actually exist.

Shared by two callers with the same question and different appetites. Discovery resolves one
claim to one place or drops it; a rider typing "Leavenworth" needs to be shown that there are
three. Same request, same failure modes, same `place_id` — so one implementation, because two
would agree today and drift by next week while both kept answering plausibly.

**This is also what makes "never invent a place" structural rather than advisory.** The
assistant's waypoint tool takes a name and calls this; there is no coordinate argument for a
fabrication to arrive in. Every other stage already works this way — extraction may only name
places present in its input, resolution turns a claim into a `place_id` or discards it — and
the waypoint tool was the one place a model's assertion became geometry unchecked.
"""

import dataclasses
from typing import Any

import httpx

from motorooter.planning.discovery.errors import (
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.routing.models import Coordinate

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

LOOKUP_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,places.types"
)
"""Identity, name, position and kind. Nothing else is shown or stored.

Narrower than the resolve mask, which also asks for ratings — those move the request to a
higher billing tier and a place-name search has no use for them.

`formattedAddress` is here because without it the multi-result design fails at the case it
exists for: three places called Leavenworth render as three identical rows, and a rider
choosing between them has nothing to choose on. Frontend raised that before building the
control, which is the right time.
"""

MAX_RESULTS = 5
"""How many candidates to offer when a name is ambiguous.

More than one on purpose: `resolve` asks for a single result because it wants a fact, and this
wants to hand back a choice. Five is enough to cover the real Leavenworths without turning a
search box into a list nobody reads.
"""

BIAS_RADIUS_M = 50_000.0
"""How far around a reference point to prefer results. Matches the resolve stage's radius —
a corridor is the same kind of hint whichever stage is asking."""


@dataclasses.dataclass(frozen=True)
class FoundPlace:
    """One real place, as Places describes it."""

    name: str
    place_id: str
    coordinate: Coordinate
    kinds: tuple[str, ...] = ()

    address: str | None = None
    """What distinguishes two places with the same name. `None` when Places has none — a
    mountain pass often does not — in which case `kinds` is what is left to show."""


class PlaceLookup:
    """Finds places by name, optionally biased toward a point."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = PLACES_SEARCH_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client = client
        self._timeout_s = timeout_s

    async def search(
        self, text: str, *, near: Coordinate | None = None, limit: int = MAX_RESULTS
    ) -> tuple[FoundPlace, ...]:
        """Places matching `text`, best first, empty if nothing matched.

        Args:
            text: what the rider or the assistant typed.
            near: bias results toward here. `None` means no bias, which is the honest answer
                for an empty trip — inventing a centre would silently prefer one of several
                real places over the others.
            limit: how many candidates to return.

        Raises:
            DiscoveryRateLimited, DiscoveryQuotaExceeded, DiscoveryRefused,
            DiscoveryUnavailable: the shared hierarchy, so callers never see a vendor shape.
        """
        payload: dict[str, Any] = {"textQuery": text, "maxResultCount": limit}
        if near is not None:
            payload["locationBias"] = {
                "circle": {
                    "center": {"latitude": near.lat, "longitude": near.lon},
                    "radius": BIAS_RADIUS_M,
                }
            }

        response = await self._post(payload)
        self._raise_for_status(response)
        return self._places_in(response)

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": LOOKUP_FIELD_MASK,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                return await self._client.post(
                    self._base_url, json=payload, headers=headers, timeout=self._timeout_s
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.post(self._base_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"Places lookup failed: {exc}"
            raise DiscoveryUnavailable(msg) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        if status == 429:
            raise DiscoveryRateLimited("Places rate limit reached (HTTP 429)")
        if status == 402:
            raise DiscoveryQuotaExceeded("Places budget exhausted (HTTP 402)")
        if status >= 500:
            raise DiscoveryUnavailable(f"Places returned HTTP {status}")
        if status == 403:
            # The same trap resolve documents: a referrer-restricted key fails server-side
            # in a way that looks nothing like a configuration problem.
            msg = (
                "Places refused the request (HTTP 403). If the key has been restricted by "
                "HTTP referrer, server-side calls cannot satisfy it — they send no referrer."
            )
            raise DiscoveryRefused(msg)
        raise DiscoveryRefused(f"Places refused the request (HTTP {status})")

    @staticmethod
    def _places_in(response: httpx.Response) -> tuple[FoundPlace, ...]:
        """Well-formed entries only. A malformed one is skipped, not fatal.

        One bad entry in a list of five should cost that entry, not the search — the same
        rule the resolve stage applies to a batch of candidates.
        """
        try:
            body = response.json()
        except ValueError:
            return ()
        entries = body.get("places") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            return ()

        found = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            place_id = entry.get("id")
            location = entry.get("location")
            display = entry.get("displayName")
            if not isinstance(place_id, str) or not isinstance(location, dict):
                continue
            latitude, longitude = location.get("latitude"), location.get("longitude")
            if not isinstance(latitude, int | float) or not isinstance(longitude, int | float):
                continue
            name = display.get("text") if isinstance(display, dict) else None
            address = entry.get("formattedAddress")
            found.append(
                FoundPlace(
                    name=name if isinstance(name, str) and name else place_id,
                    place_id=place_id,
                    coordinate=Coordinate(lat=float(latitude), lon=float(longitude)),
                    kinds=tuple(k for k in (entry.get("types") or []) if isinstance(k, str)),
                    address=address if isinstance(address, str) and address else None,
                )
            )
        return tuple(found)
