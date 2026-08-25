"""Trip CRUD, plus the not-yet-implemented replan and export endpoints.

Trips are public and unauthenticated by design for the prototype: anyone with the link can
read or edit. Creation refuses to clobber an existing slug; replacement is an explicit PUT.

The 501 endpoints are deliberate. Their schemas are fully defined so they appear in the
OpenAPI document and generate TypeScript types, letting the frontend build against the real
shapes before the backend implementation lands.
"""

from fastapi import APIRouter, status

from motorooter.api.deps import Trips
from motorooter.api.errors import NotImplementedYet
from motorooter.api.schemas import (
    ERROR_RESPONSES,
    CreateTripRequest,
    ErrorResponse,
    ReplanEvent,
    ReplanRequest,
    UpdateTripRequest,
)
from motorooter.trips.errors import TripModifiedConcurrently
from motorooter.trips.models import Trip, TripSummary, utc_now
from motorooter.trips.slug import slugify, validate_slug

router = APIRouter(prefix="/api/trips", tags=["trips"], responses=ERROR_RESPONSES)

NOT_IMPLEMENTED = status.HTTP_501_NOT_IMPLEMENTED


STREAMING_MEDIA_TYPE = "application/x-ndjson"
"""Wire format for the replan stream. Applied to the document by `api.streaming`."""


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


MAX_UPDATE_ATTEMPTS = 2
"""Read-merge-write tries twice before reporting a conflict.

One retry resolves the ordinary case — two riders editing different fields of the same
shared trip — because re-merging a partial request onto the newer document yields the union
of both edits. A writer that loses twice is contending with sustained traffic, and looping
further would spend requests without converging.
"""


def _merge(existing: Trip, request: UpdateTripRequest) -> Trip:
    """Apply a partial update to a trip.

    `edited_at` advances only when geometry actually changes, since it drives the replan
    staleness flag — bumping it on a rename would spuriously mark discovery stale.
    """
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
    # Revalidate: model_copy skips validators, and leg/waypoint consistency lives there.
    return Trip.model_validate(updated.model_dump())


@router.put("/{slug}", response_model=Trip)
async def update_trip(slug: str, request: UpdateTripRequest, store: Trips) -> Trip:
    """Apply a partial update, refusing to clobber a concurrent edit.

    Trips are public and world-editable, so two riders editing the same trip from a shared
    link is ordinary. An unconditional write would not merely lose the slower writer's edit —
    it would roll back fields that writer never touched, and answer 200 as though the data
    had been saved. So the write carries the version it was read at, and a conflict re-reads
    and re-merges rather than surfacing immediately.

    Raises:
        TripModifiedConcurrently: still contended after `MAX_UPDATE_ATTEMPTS`; maps to 409.
        TripNotFound: no such trip, including one deleted mid-update — writing anyway would
            resurrect something somebody chose to remove.
    """
    slug = validate_slug(slug)

    for attempt in range(1, MAX_UPDATE_ATTEMPTS + 1):
        versioned = await store.get_versioned(slug)
        merged = _merge(versioned.trip, request)
        try:
            return await store.put(merged, if_version=versioned.version)
        except TripModifiedConcurrently:
            if attempt == MAX_UPDATE_ATTEMPTS:
                raise
    raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(slug: str, store: Trips) -> None:
    await store.delete(validate_slug(slug))


@router.post(
    "/{slug}/replan",
    status_code=NOT_IMPLEMENTED,
    summary="Start a replan (not yet implemented)",
    description=(
        "Runs LLM route search, POI discovery, and Places enrichment.\n\n"
        "**Streams newline-delimited JSON** (`application/x-ndjson`): one `ReplanEvent` "
        "object per line, terminated by `\\n`. Not Server-Sent Events — this is a POST with "
        "a request body, so `EventSource` cannot consume it, and hand-parsing SSE framing "
        "over `fetch` would cost the framing overhead for none of the benefit. Clients must "
        "tolerate a chunk boundary landing mid-line.\n\n"
        "Explicitly user-triggered — never fired automatically by a route edit."
    ),
    responses={
        # `model` rather than a raw $ref: FastAPI only emits a schema into components when
        # a model is referenced this way. A bare $ref would leave ReplanEvent out of the
        # document entirely and silently delete the frontend's generated type.
        200: {
            # Declared as an ordinary model; api.streaming rewrites the media-type key to
            # STREAMING_MEDIA_TYPE after generation. See that module for why.
            "description": "Stream of ReplanEvent objects, one per line.",
            "model": ReplanEvent,
        },
        501: {"model": ErrorResponse, "description": "Not implemented yet."},
    },
)
async def replan(slug: str, request: ReplanRequest, store: Trips) -> None:
    """Reserved. Owned by the backend engineer; schema is frozen so the frontend can build."""
    # 404 before 501, so the frontend can distinguish "no such trip" from "not built yet".
    await store.get(validate_slug(slug))
    raise NotImplementedYet("replan")


@router.get(
    "/{slug}/gpx",
    status_code=NOT_IMPLEMENTED,
    summary="Export GPX (not yet implemented)",
    description=(
        "Returns a GPX file containing a track plus ordered waypoints, targeted at "
        "motorcycle GPS units."
    ),
    responses={
        200: {"description": "GPX file.", "content": {"application/gpx+xml": {}}},
        501: {"model": ErrorResponse},
    },
)
async def export_gpx(slug: str, store: Trips) -> None:
    """Reserved. Owned by the backend engineer."""
    await store.get(validate_slug(slug))
    raise NotImplementedYet("GPX export")
