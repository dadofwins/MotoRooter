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
from motorooter.api.errors import NotImplementedYet
from motorooter.api.schemas import ERROR_RESPONSES, ErrorResponse, PoiDetailResponse
from motorooter.planning.discovery.category import from_places_types
from motorooter.planning.discovery.details import coordinate_in, strings_in
from motorooter.planning.discovery.errors import DiscoveryRefused
from motorooter.trips.models import Poi, PoiCategory, PoiDetail, PoiSource

router = APIRouter(prefix="/api/places", tags=["places"], responses=ERROR_RESPONSES)


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
        raise DiscoveryRefused(f"Places returned no location for {place_id!r}")

    resolved_category = from_places_types(body.get("types") or []) or category
    if resolved_category is None:
        raise DiscoveryRefused(
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
