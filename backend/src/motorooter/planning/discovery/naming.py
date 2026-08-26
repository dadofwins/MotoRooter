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
import re
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

**But only when the road has a name worth searching**, which is why `route` is qualified by
`is_distinctive_road` rather than taken outright. The same ordering unqualified produced
`West Davis Street` and `Cottage Avenue` in a valley, and a street name that exists in every
town in America returns pages about anywhere. Measured over two corridors, counting
candidates that survived the distance filter:

    rule                        Chinook   Ellensburg-Cashmere
    route above locality              8                     1
    locality above route              4                     5
    distinctive routes only           7                     6

Inverting the order is not the fix — it trades one corridor for the other, exactly as the
Enumclaw reasoning predicts. Qualifying the road keeps both.

A county is never useful — "camping near Yakima County" returns a different and much worse
set of pages than "camping near Chinook Pass".
"""

_REGION_TYPE = "administrative_area_level_1"

_PLUS_CODE = re.compile(r"^[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}$")
"""An Open Location Code, which Google returns as a name when it has nothing else.

Matched on the code's own alphabet — it deliberately omits vowels and easily-confused
characters — rather than on "contains a plus", so a real name with a `+` in it survives.

Worth refusing rather than searching: `84VX9FP2+WM` is a coordinate in eleven characters, and
handing it to a web search buys nothing for the metered request it costs. Not hypothetical —
one anchor per corridor came back like this on the Ellensburg-Cashmere run.
"""


def is_plus_code(name: str) -> bool:
    """Whether this "name" is really a coordinate."""
    return bool(_PLUS_CODE.match(name.strip()))


_DESIGNATED_ROAD = re.compile(
    r"\b(parkway|pkwy|highway|hwy|freeway|expressway|pass|byway|scenic|trail|route|road)\s*\d",
    re.I,
)
_NAMED_ROAD_WORDS = re.compile(
    r"\b(parkway|pkwy|highway|hwy|freeway|expressway|turnpike|pass|byway|scenic|trail|"
    r"forest|canyon|ridge|creek\s+road|river\s+road|loop)\b",
    re.I,
)


def is_distinctive_road(name: str) -> bool:
    """Whether a road name is worth searching for, or is just somebody's street.

    Two signals, and the first does most of the work. **A number makes a road a designation**
    — `U.S. 12`, `Washington 123`, `State Route 20` — and numbering highways is close to
    universal, so this half carries over to countries whose street words we do not know.

    The word list is the English-only half, for roads that are named rather than numbered:
    `Mather Memorial Parkway`, `Chinook Pass to Tipsoo Lake Trail`. It is a supplement, not
    the mechanism, and its failure is graceful in a way a street-suffix denylist would not
    be: an unrecognised name falls through to the locality, which is a usable search term.
    A denylist that failed to recognise `Rue` or `Strasse` would instead keep searching for
    it, which is the bug being fixed.

    So a false negative costs precision and a false positive costs a corridor. This errs
    towards the locality on purpose.
    """
    stripped = name.strip()
    if not stripped:
        return False
    if any(character.isdigit() for character in stripped):
        return True
    return bool(_DESIGNATED_ROAD.search(stripped) or _NAMED_ROAD_WORDS.search(stripped))


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

        generic_road: str | None = None
        components = found.get("address_components")
        if isinstance(components, list):
            for wanted in _NAME_TYPES:
                name = _component(components, wanted)
                if not name:
                    continue
                # A road only outranks the town it is in when it has a name worth searching.
                # `Mather Memorial Parkway` does; `Cottage Avenue` does not, and anchoring a
                # corridor on one returned leads a median of 279 km away.
                if wanted == "route" and not is_distinctive_road(name):
                    generic_road = name
                    continue
                return name
            # Nothing better turned up, so the street is what there is. A weak search term
            # beats losing the stretch's searches altogether.
            if generic_road is not None:
                return generic_road

        # Better a rough name than no search at all for that stretch of route — but only if
        # it is a name. A remote coordinate's `formatted_address` is often just a plus code,
        # and searching for one is what the first line of this docstring rules out.
        formatted = found.get("formatted_address")
        if not isinstance(formatted, str):
            return None
        rough = formatted.split(",")[0].strip()
        return None if not rough or is_plus_code(rough) else rough

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
