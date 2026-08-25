"""Trip CRUD, plus the not-yet-implemented replan and export endpoints.

Trips are public and unauthenticated by design for the prototype: anyone with the link can
read or edit. Creation refuses to clobber an existing slug; replacement is an explicit PUT.

The 501 endpoints are deliberate. Their schemas are fully defined so they appear in the
OpenAPI document and generate TypeScript types, letting the frontend build against the real
shapes before the backend implementation lands.
"""

from fastapi import APIRouter, status

from motorooter.api.deps import Trips
from motorooter.api.schemas import (
    ERROR_RESPONSES,
    CreateTripRequest,
    ReplanEvent,
    ReplanRequest,
    UpdateTripRequest,
)
from motorooter.trips.models import Trip, TripSummary, utc_now
from motorooter.trips.slug import slugify, validate_slug

router = APIRouter(prefix="/api/trips", tags=["trips"], responses=ERROR_RESPONSES)

NOT_IMPLEMENTED = status.HTTP_501_NOT_IMPLEMENTED


@router.get("", response_model=list[TripSummary])
async def list_trips(store: Trips) -> list[TripSummary]:
    return await store.list()


@router.post("", response_model=Trip, status_code=status.HTTP_201_CREATED)
async def create_trip(request: CreateTripRequest, store: Trips) -> Trip:
    """Create an empty trip.

    An explicit slug is validated strictly; a derived one is slugified from the name.
    Either way it is checked before it can become a storage path.
    """
    slug = validate_slug(request.slug) if request.slug is not None else slugify(request.name)
    now = utc_now()
    return await store.create(Trip(slug=slug, name=request.name, created_at=now, edited_at=now))


@router.get("/{slug}", response_model=Trip)
async def get_trip(slug: str, store: Trips) -> Trip:
    return await store.get(validate_slug(slug))


@router.put("/{slug}", response_model=Trip)
async def update_trip(slug: str, request: UpdateTripRequest, store: Trips) -> Trip:
    """Replace a trip's editable content.

    `edited_at` advances only when geometry actually changes, since it is what drives the
    replan staleness flag — bumping it on a rename would spuriously mark discovery stale.
    """
    slug = validate_slug(slug)
    existing = await store.get(slug)

    geometry_changed = (
        request.waypoints is not None and tuple(request.waypoints) != existing.waypoints
    ) or (request.legs is not None and tuple(request.legs) != existing.legs)

    updated = existing.model_copy(
        update={
            "name": request.name if request.name is not None else existing.name,
            "waypoints": tuple(request.waypoints)
            if request.waypoints is not None
            else existing.waypoints,
            "legs": tuple(request.legs) if request.legs is not None else existing.legs,
            "pois": tuple(request.pois) if request.pois is not None else existing.pois,
            "edited_at": utc_now() if geometry_changed else existing.edited_at,
        }
    )
    # Revalidate: model_copy skips validators, and leg/waypoint consistency is enforced there.
    return await store.put(Trip.model_validate(updated.model_dump()))


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(slug: str, store: Trips) -> None:
    await store.delete(validate_slug(slug))


@router.post(
    "/{slug}/replan",
    status_code=NOT_IMPLEMENTED,
    response_model=ReplanEvent,
    summary="Start a replan (not yet implemented)",
    description=(
        "Runs LLM route search, POI discovery, and Places enrichment, streaming "
        "Server-Sent Events whose data is a ReplanEvent. Explicitly user-triggered — never "
        "fired automatically by a route edit."
    ),
)
async def replan(slug: str, request: ReplanRequest, store: Trips) -> None:
    """Reserved. Owned by the backend engineer; schema is frozen so the frontend can build."""
    from fastapi import HTTPException

    await store.get(validate_slug(slug))  # 404 before 501, so the frontend can tell them apart
    raise HTTPException(NOT_IMPLEMENTED, detail="replan is not implemented yet")


@router.get(
    "/{slug}/gpx",
    status_code=NOT_IMPLEMENTED,
    summary="Export GPX (not yet implemented)",
    description=(
        "Returns a GPX file containing a track plus ordered waypoints, targeted at "
        "motorcycle GPS units."
    ),
    responses={200: {"content": {"application/gpx+xml": {}}}},
)
async def export_gpx(slug: str, store: Trips) -> None:
    """Reserved. Owned by the backend engineer."""
    from fastapi import HTTPException

    await store.get(validate_slug(slug))
    raise HTTPException(NOT_IMPLEMENTED, detail="GPX export is not implemented yet")
