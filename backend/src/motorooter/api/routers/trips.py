"""Trip CRUD, plus the not-yet-implemented replan and export endpoints.

Trips are public and unauthenticated by design for the prototype: anyone with the link can
read or edit. Creation refuses to clobber an existing slug; replacement is an explicit PUT.

The 501 endpoints are deliberate. Their schemas are fully defined so they appear in the
OpenAPI document and generate TypeScript types, letting the frontend build against the real
shapes before the backend implementation lands.
"""

import logging
from collections.abc import AsyncIterator, Sequence

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse

from motorooter.api.deps import Discovery, Trips
from motorooter.api.errors import NotImplementedYet
from motorooter.api.schemas import (
    ERROR_RESPONSES,
    ChatEvent,
    ChatRequest,
    CreateTripRequest,
    ErrorResponse,
    ReplanEvent,
    ReplanRequest,
    UpdateTripRequest,
)
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.routing.errors import RouteIncomplete
from motorooter.routing.models import RouteLeg
from motorooter.trips.models import PoiCategory, Trip, TripSummary, utc_now
from motorooter.trips.service import edit_trip
from motorooter.trips.slug import slugify, validate_slug

router = APIRouter(prefix="/api/trips", tags=["trips"], responses=ERROR_RESPONSES)

NOT_IMPLEMENTED = status.HTTP_501_NOT_IMPLEMENTED

logger = logging.getLogger(__name__)


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
    return await edit_trip(
        store,
        validate_slug(slug),
        name=request.name,
        waypoints=request.waypoints,
        legs=request.legs,
        pois=request.pois,
    )


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(slug: str, store: Trips) -> None:
    await store.delete(validate_slug(slug))


@router.post(
    "/{slug}/replan",
    response_model=ReplanEvent,
    summary="Discover points of interest along the route",
    description=(
        "Runs web search, place extraction, Places resolution and scoring over the trip's "
        "corridor. Streams application/x-ndjson: one ReplanEvent per line, POIs "
        "accumulating as they resolve so the map fills in rather than waiting. Not "
        "Server-Sent Events — there is no `data:` prefix and no blank-line framing. "
        "Explicitly user-triggered; never fired automatically by a route edit. Answers 501 "
        "when the discovery credentials are not configured."
    ),
    responses={NOT_IMPLEMENTED: {"model": ErrorResponse}},
)
async def replan(
    slug: str, request: ReplanRequest, store: Trips, discovery: Discovery
) -> StreamingResponse:
    """Stream discovery progress for a trip.

    The trip is fetched before the stream opens so a missing slug is an ordinary 404 with a
    JSON body. Once the stream is open the status is already sent, and a failure can only be
    reported as an event inside it — which is why everything that can be checked up front is.
    """
    trip = await store.get(validate_slug(slug))
    if discovery is None:
        raise NotImplementedYet("discovery (no search, model or Places credentials configured)")

    leg = _longest_routed_leg(trip)
    if leg is None:
        raise RouteIncomplete(trip.unrouted_leg_indices or (0,))

    return StreamingResponse(
        _stream(discovery, leg, request.categories),
        media_type=STREAMING_MEDIA_TYPE,
    )


def _longest_routed_leg(trip: Trip) -> RouteLeg | None:
    """The leg worth searching along.

    The longest rather than the first: a trip's legs are frequently one long ride and a short
    connector, and discovery along the connector would search the wrong half of the map.
    """
    routed = trip.routed_legs
    return max(routed, key=lambda leg: leg.distance_m) if routed else None


async def _stream(
    discovery: DiscoveryPipeline, leg: RouteLeg, categories: Sequence[PoiCategory]
) -> AsyncIterator[bytes]:
    """One `ReplanEvent` per line.

    Errors after the first byte cannot change the status code, so an unexpected failure is
    emitted as a final event rather than allowed to truncate the stream. A client that sees
    the connection close mid-line cannot tell a crash from a network drop; one that reads
    `stage: done` with a failure message can.
    """
    try:
        async for step in discovery.run(leg, list(categories)):
            event = ReplanEvent(
                stage=step.stage,
                message=step.message,
                progress=step.progress,
                pois=list(step.pois),
            )
            yield event.model_dump_json().encode() + b"\n"
    except Exception:
        logger.exception("replan stream failed")
        failed = ReplanEvent(
            stage="done",
            message="discovery stopped early after an unexpected error",
            progress=1.0,
        )
        yield failed.model_dump_json().encode() + b"\n"


@router.post(
    "/{slug}/chat",
    status_code=NOT_IMPLEMENTED,
    summary="Talk to the assistant about a trip (not yet implemented)",
    description=(
        "One conversational turn. The assistant may call tools, and those tools are the "
        "same service functions the mouse path calls — item 5 of the MVP is reachable both "
        "ways and must not become two implementations that drift.\n\n"
        "**Streams newline-delimited JSON** (`application/x-ndjson`): one `ChatEvent` per "
        "line, `done` last. Same framing as replan, which the client already parses.\n\n"
        "The trip is addressed by slug rather than sent, so the assistant edits the same "
        "document the map does; when `trip_changed` is set the client re-reads it."
    ),
    responses={
        200: {
            "description": "Stream of ChatEvent objects, one per line.",
            "model": ChatEvent,
        },
        501: {"model": ErrorResponse, "description": "Not implemented yet."},
    },
)
async def chat(slug: str, request: ChatRequest, store: Trips) -> None:
    """Reserved. Schema frozen so the chat rail can be built before the endpoint exists."""
    await store.get(validate_slug(slug))
    raise NotImplementedYet("chat")


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
