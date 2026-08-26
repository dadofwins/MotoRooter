"""Turning a claimed name into a real place, or dropping it.

The stage where a claim becomes a fact. Everything upstream is untrusted: a web page said a
campsite exists, and a model read the name out of the prose. Places decides whether the thing
is real, where it actually is, and therefore whether it may be shown to anyone.

**A candidate that will not resolve is dropped, never guessed at.** `Poi` already refuses to
pin an unverified suggestion, so the invariant holds either way — but discarding is the right
behaviour rather than pinning something plausible at an invented coordinate.

**Distance is the relevance filter, and this is the only stage that can apply it.** Extraction
cannot: on the Chinook Pass corridor it produced Miller Peak and Stafford Creek, both real,
both correctly identified as Washington, and both about 100 km away in the Teanaway. "In the
right state" is the finest judgement available from text alone, and a corridor is tens of
metres wide. The filter has to be arithmetic, and arithmetic needs the coordinate that only
this stage produces.

**Only `place_id` is persisted.** Google's terms permit storing that indefinitely and very
little else, so ratings and photos are not requested here at all — the field mask also
determines the billing tier, so asking for data we may not keep would cost money for nothing.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Sequence
from typing import Any

import httpx

from motorooter.planning.discovery.category import from_places_types
from motorooter.planning.discovery.concurrency import DEFAULT_CONCURRENCY
from motorooter.planning.discovery.errors import (
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.planning.discovery.models import Candidate, ResolvedCandidate
from motorooter.planning.metrics import nearest_distance_m
from motorooter.routing.models import Coordinate

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _Place:
    """What one Places match gave us."""

    place_id: str
    coordinate: Coordinate
    types: tuple[str, ...] = ()
    rating: float | None = None
    user_rating_count: int | None = None


PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

FIELD_MASK = (
    "places.id,places.displayName,places.location,places.types,places.rating,places.userRatingCount"
)
"""Exactly what is used, and nothing more.

Mandatory on this API, and it selects the billing tier. `rating` and `userRatingCount` move
the request to a higher SKU than id-and-location alone, which is a deliberate trade: a rating
is a *fact* about whether a place is worth stopping at, and handing the judge a fact beats
asking a model to guess one. Cost is explicitly not a constraint on this prototype.

`types` is what a place actually *is*, and it is the reason a ski resort no longer arrives
tagged as a wild camp. Deterministic and free next to the alternative, which is asking a
model.

Photos, reviews and opening hours stay out. They cannot be stored under Google's terms and
nothing displays them yet, so requesting them would raise the tier again to fetch data that
is thrown away.
"""

DEFAULT_CORRIDOR_M = 15_000.0
"""How far off the route a place may sit and still count as on the way.

Generous on purpose. A rider will detour fifteen minutes for a good camp or a hot meal, and
the *cost* of the detour is evidence for the judge rather than a reason to hide the option.
This only rejects the gross errors — the campsite in the next mountain range.

A guess, like the other thresholds here, and an argument rather than a constant.
"""

SEARCH_BIAS_RADIUS_M = 50_000.0
"""Radius of the location bias sent with each lookup. Place names repeat across a continent;
the corridor is what disambiguates them."""


class PlacesResolver:
    """Resolves candidate names against Google Places."""

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

    async def resolve(
        self,
        candidates: Sequence[Candidate],
        *,
        route: Sequence[Coordinate] = (),
        corridor_m: float = DEFAULT_CORRIDOR_M,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> tuple[ResolvedCandidate, ...]:
        """Resolve each candidate, dropping whatever does not survive.

        Args:
            candidates: claims from search and extraction.
            route: the corridor. Empty means no distance filtering — resolving a single name
                without a route is legitimate.
            corridor_m: how far off the route a place may sit.
            concurrency: lookups in flight at once.

        Concurrent, but bounded. A corridor yields dozens of names and each is one metered
        lookup, so doing them in turn left the slowest stretch of discovery in place after
        the rest was fixed. The bound is the point: Places rate-limits, and thirty at once is
        what trips it.

        Results keep the order they were given in. Completion order is a race, and a list
        that reshuffles between two runs over the same corridor looks broken.
        """
        limit = asyncio.Semaphore(max(concurrency, 1))

        async def resolve_one(candidate: Candidate) -> ResolvedCandidate | None:
            async with limit:
                found = await self._lookup(candidate)
            if found is None:
                return None

            distance = nearest_distance_m(route, found.coordinate) if route else None
            if distance is not None and distance > corridor_m:
                logger.info("dropped %r: %.0f m off the corridor", candidate.name, distance)
                return None

            return ResolvedCandidate(
                candidate=candidate,
                place_id=found.place_id,
                coordinate=found.coordinate,
                # From what Places says it is, never from the query that found it.
                category=from_places_types(found.types),
                places_types=found.types,
                rating=found.rating,
                user_rating_count=found.user_rating_count,
                distance_off_route_m=distance,
            )

        # gather rather than as-completed: it preserves input order for free, and there is
        # nothing to stream to — the caller wants the whole set before judging.
        settled = await asyncio.gather(*(resolve_one(candidate) for candidate in candidates))
        return tuple(item for item in settled if item is not None)

    async def _lookup(self, candidate: Candidate) -> _Place | None:
        payload: dict[str, Any] = {
            "textQuery": candidate.name,
            "maxResultCount": 1,
            # Names repeat across a continent. The anchor is the disambiguator, and it is
            # our coordinate rather than anything a source claimed.
            "locationBias": {
                "circle": {
                    "center": {
                        "latitude": candidate.found_near.lat,
                        "longitude": candidate.found_near.lon,
                    },
                    "radius": SEARCH_BIAS_RADIUS_M,
                }
            },
        }
        response = await self._post(payload)
        self._raise_for_status(response)
        return self._first_place(response)

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": FIELD_MASK,
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
            msg = f"Places request failed: {exc}"
            raise DiscoveryUnavailable(msg) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code

        if status == 403:
            # Worth its own words. The server key is currently the same value as the
            # browser key, and a browser key ships in the bundle so it should be
            # referrer-restricted before any public deploy. Server requests carry no
            # referrer, so the day that restriction lands this starts failing with
            # something that looks nothing like a config change.
            msg = (
                "Places refused the request (HTTP 403). If the API key has been restricted "
                "by HTTP referrer, server-side calls cannot satisfy it — they send no "
                "referrer. A separate, unrestricted server key is needed."
            )
            raise DiscoveryRefused(msg)
        if status == 429:
            msg = "Places rate limit reached (HTTP 429)"
            raise DiscoveryRateLimited(msg)
        if status == 402:
            msg = "Places billing quota exhausted (HTTP 402)"
            raise DiscoveryQuotaExceeded(msg)
        if status >= 500:
            msg = f"Places returned HTTP {status}"
            raise DiscoveryUnavailable(msg)
        msg = f"Places rejected the request (HTTP {status})"
        raise DiscoveryRefused(msg)

    @staticmethod
    def _first_place(response: httpx.Response) -> "_Place | None":
        """The first usable match, or `None`.

        Anything missing an id or a location is treated as no match rather than an error: a
        place that cannot be persisted or cannot be put on a map has not been resolved,
        whatever else came back with it.
        """
        try:
            body = response.json()
        except ValueError as exc:
            msg = "Places returned a body that was not JSON"
            raise DiscoveryUnavailable(msg) from exc

        if not isinstance(body, dict):
            return None
        places = body.get("places")
        if not isinstance(places, list) or not places:
            return None
        first = places[0]
        if not isinstance(first, dict):
            return None

        place_id = first.get("id")
        location = first.get("location")
        if not isinstance(place_id, str) or not place_id or not isinstance(location, dict):
            return None

        lat, lon = location.get("latitude"), location.get("longitude")
        if not isinstance(lat, int | float) or not isinstance(lon, int | float):
            return None
        if isinstance(lat, bool) or isinstance(lon, bool):
            return None

        try:
            coordinate = Coordinate(lat=float(lat), lon=float(lon))
        except ValueError:
            # Out of range. Places should not do this, but a coordinate that fails the
            # domain's own validation is not one to put on a map.
            return None

        raw_types = first.get("types")
        return _Place(
            place_id=place_id,
            coordinate=coordinate,
            types=tuple(entry for entry in raw_types if isinstance(entry, str))
            if isinstance(raw_types, list)
            else (),
            rating=_number(first.get("rating"), 0.0, 5.0),
            user_rating_count=_count(first.get("userRatingCount")),
        )


def _number(value: Any, low: float, high: float) -> float | None:  # noqa: ANN401 -- raw JSON
    """A rating, if it is one. Out-of-range values are dropped rather than clamped: a 7-star
    rating means the field is not what we think it is, and inventing a 5 would hide that."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if low <= value <= high else None


def _count(value: Any) -> int | None:  # noqa: ANN401 -- raw JSON
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None
