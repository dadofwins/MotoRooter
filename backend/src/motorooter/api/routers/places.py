"""Google Places enrichment.

Reserved for the backend engineer. Schema is frozen so the frontend can build the POI
dialog against real types.

Constraint for whoever implements this: Google's terms permit storing `place_id`
indefinitely and very little else. Ratings, photos, and reviews are fetched per request
and must not be written to the trip document — which is why `PoiDetail` is a
response-only type with no persistence path.
"""

from fastapi import APIRouter, HTTPException, status

from motorooter.api.schemas import ERROR_RESPONSES, PoiDetailResponse

router = APIRouter(prefix="/api/places", tags=["places"], responses=ERROR_RESPONSES)


@router.get(
    "/{place_id}",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    response_model=PoiDetailResponse,
    summary="Fetch POI display data (not yet implemented)",
)
async def get_place_detail(place_id: str) -> None:
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED, detail="Places enrichment is not implemented yet"
    )
