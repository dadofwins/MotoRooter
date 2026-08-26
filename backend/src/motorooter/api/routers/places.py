"""Google Places enrichment: display data for the POI dialog.

Google's terms permit storing `place_id` indefinitely and very little else, so everything
here is fetched per request and written down nowhere. `PoiDetail` is a response-only type
with no persistence path, and there is deliberately no server-side cache — the frontend
asserts the behaviour by reopening the dialog and expecting a second request, and a cache
would defeat both their test and the terms it encodes.

Absence is ordinary rather than an error. Dispersed camping is what this app cares most
about and what Places knows least about: no rating, no hours, no photographs. A dialog with
a name and a location is a valid answer.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from motorooter.api.deps import Places
from motorooter.api.errors import NotImplementedYet, PlaceNotDisplayable
from motorooter.api.schemas import ERROR_RESPONSES, ErrorResponse, PoiDetailResponse
from motorooter.planning.discovery.category import from_places_types
from motorooter.planning.discovery.details import coordinate_in, strings_in
from motorooter.trips.models import Poi, PoiCategory, PoiDetail, PoiSource

router = APIRouter(prefix="/api/places", tags=["places"], responses=ERROR_RESPONSES)

MAX_PHOTOS = 3
"""How many photo URLs to hand a client per place.

Each one costs a request when the browser loads it, every time the dialog opens, and Places
returns up to ten. Three is enough for a dialog and cheap enough not to think about — the
same reasoning that kept photos out of the field mask entirely, applied to the count rather
than to the feature.
"""

PHOTO_MEDIA_URL = "https://places.googleapis.com/v1"
PHOTO_MAX_WIDTH_PX = 800
"""Wide enough for a dialog on a high-density screen, narrow enough not to ship a 3000 px
original to a phone in a car park."""


@router.get(
    "/{place_id}",
    response_model=PoiDetailResponse,
    summary="Fetch POI display data",
    description=(
        "Live Places data for the POI dialog. Never cached server-side: Google's terms "
        "permit storing `place_id` and little else, so ratings, hours and reviews are "
        "fetched per request. Fields are frequently absent — dispersed camping in "
        "particular — and that is a normal response rather than an error."
    ),
    responses={501: {"model": ErrorResponse}},
)
async def get_place_detail(
    place_id: str,
    places: Places,
    category: Annotated[
        PoiCategory | None,
        Query(
            description=(
                "The category the client already holds, used only when Places' own types "
                "do not map to one. Never overrides what Places says."
            )
        ),
    ] = None,
) -> PoiDetailResponse:
    """Display data for one place.

    The category is taken from Places where it can be, exactly as at resolution — the query
    parameter is the fallback rather than the source, because inheriting a category from a
    caller is how a ski resort came to be tagged as a wild camp.
    """
    if places is None:
        raise NotImplementedYet("Places enrichment (no Places credentials configured)")

    body = await places.fetch(place_id)

    coordinate = coordinate_in(body)
    if coordinate is None:
        # Without a location there is nothing to show on a map or pin to a route.
        raise PlaceNotDisplayable(f"Places returned no location for {place_id!r}")

    resolved_category = from_places_types(body.get("types") or []) or category
    if resolved_category is None:
        raise PlaceNotDisplayable(
            f"cannot categorise {place_id!r}: Places gave no usable type and no category "
            "was supplied. Pass ?category= with the value the client already holds."
        )

    display = body.get("displayName")
    name = display.get("text") if isinstance(display, dict) else None
    hours = body.get("regularOpeningHours")

    return PoiDetailResponse(
        detail=PoiDetail(
            poi=Poi(
                id=place_id,
                name=name if isinstance(name, str) and name else place_id,
                category=resolved_category,
                coordinate=coordinate,
                source=PoiSource.PLACES,
                place_id=place_id,
            ),
            rating=_rating(body.get("rating")),
            user_rating_count=_count(body.get("userRatingCount")),
            reviews=_reviews(body.get("reviews")),
            website=_text(body.get("websiteUri")),
            phone=_text(body.get("nationalPhoneNumber")),
            opening_hours=strings_in(
                hours.get("weekdayDescriptions") if isinstance(hours, dict) else None
            ),
            photo_urls=_photo_urls(body.get("photos"), key=places.photo_key),
        )
    )


def _rating(value: object) -> float | None:
    """A rating in range, or nothing. Out of range means the field is not what we think."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if 0.0 <= value <= 5.0 else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _photo_urls(value: object, *, key: str) -> tuple[str, ...]:
    """Photo references as URLs a browser can load directly.

    A resolved media URL, not a reference and not a URL the client has to complete. The
    alternatives were the question frontend asked, and both are worse: handing over a bare
    reference makes the client construct Google URLs, and handing over a URL needing a key
    appended means publishing the server key to an unauthenticated page.

    **The key it carries is the photo key, not the search key.** A URL handed to a browser
    publishes whatever key is in it, and the search-side key also authorises Directions,
    Geocoding and Places Text Search with no ceiling — so the two must not be the same value
    on anything deployed. `PlaceDetails.photo_key` falls back to the server key when only one
    is configured, which keeps a prototype working and is announced at startup rather than
    assumed.

    Not cached anywhere: Google's terms permit storing `place_id` and little else, so these
    are rebuilt per request like every other field on `PoiDetail`.
    """
    if not isinstance(value, list):
        return ()
    urls = []
    # Filtered before capped, not after: slicing first lets malformed entries spend the
    # budget, so a place whose first three photos were junk would show none at all.
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        urls.append(
            f"{PHOTO_MEDIA_URL}/{name.strip()}/media?maxWidthPx={PHOTO_MAX_WIDTH_PX}&key={key}"
        )
        if len(urls) == MAX_PHOTOS:
            break
    return tuple(urls)


def _reviews(value: object) -> tuple[str, ...]:
    """Review text only.

    Deliberately just the prose: author names and profile photos are personal data with no
    use in this dialog, and not requesting them keeps them out of logs and memory entirely.
    """
    if not isinstance(value, list):
        return ()
    texts = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        body = text.get("text") if isinstance(text, dict) else text
        if isinstance(body, str) and body.strip():
            texts.append(body.strip())
    return tuple(texts)
