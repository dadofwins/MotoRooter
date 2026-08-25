"""Turning a place and a category into a web search.

Search is the only stage that can discover a road is *good*. Places will tell you a
restaurant exists and what it is rated; it will not tell you that locals ride a pass for the
pleasure of it, or that a forest road washes out in spring. That knowledge exists in ride
reports, forum threads and BDR guides, and the query is what reaches it — which makes the
wording part of the product rather than plumbing.

Two things follow. The query must name a *place*, because a web index has no idea what
47.0,-121.0 is; turning an anchor into a name is a prerequisite stage, not an optimisation.
And it must say motorcycle, or the results are car camping and family restaurants.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from motorooter.trips.models import PoiCategory

CATEGORY_TEMPLATES: MappingProxyType[PoiCategory, str] = MappingProxyType(
    {
        # Phrased the way a rider would ask, because that is what matches the pages worth
        # finding. "dispersed camping" is the term the forums actually use.
        PoiCategory.WILD_CAMP: "dispersed camping near {place} motorcycle",
        PoiCategory.CAMPGROUND: "motorcycle camping campground near {place}",
        PoiCategory.HOTEL: "motorcycle friendly hotel {place}",
        PoiCategory.UNIQUE_STAY: "unusual places to stay near {place} motorcycle trip",
        PoiCategory.FOOD: "best motorcycle rider food stop near {place}",
        PoiCategory.FUEL: "fuel station {place} motorcycle route",
        PoiCategory.WATER: "drinking water refill near {place} motorcycle camping",
        PoiCategory.VIEWPOINT: "scenic viewpoint near {place} motorcycle ride",
        PoiCategory.MECHANIC: "motorcycle repair shop near {place}",
    }
)
"""One query per category. A missing entry would silently search nothing for it, so the
completeness of this table is asserted in the tests rather than trusted."""


@dataclass(frozen=True)
class SearchQuery:
    """One search to run, carrying where it came from.

    The category travels with the query so a result can be labelled without inferring it
    from the text, which is the sort of guess that puts a fuel station in the camping list.
    """

    text: str
    category: PoiCategory
    place: str


def queries_for(place: str, categories: Iterable[PoiCategory]) -> tuple[SearchQuery, ...]:
    """One query per distinct category, for a named place.

    Deduplicated: anchors multiplied by categories is the metered request count, and a caller
    passing a category twice should not pay for it twice.

    Raises:
        ValueError: the place is blank, which would spend a request on a query with no
            location in it at all.
    """
    if not place.strip():
        msg = "place must not be blank; anchors are named before they are searched"
        raise ValueError(msg)

    seen: list[PoiCategory] = []
    for category in categories:
        if category not in seen:
            seen.append(category)

    return tuple(
        SearchQuery(
            text=CATEGORY_TEMPLATES[category].format(place=place.strip()),
            category=category,
            place=place.strip(),
        )
        for category in seen
    )


def total_queries(anchors: Sequence[object], categories: Iterable[PoiCategory]) -> int:
    """What a corridor will cost in search requests, before spending any.

    Worth being able to ask cheaply: the fan-out is the product of two numbers that each look
    small on their own, and forty anchors across nine categories is 360 metered requests.
    """
    distinct = len(set(categories))
    return len(anchors) * distinct
