"""Trip CRUD, plus the not-yet-implemented replan and export endpoints.

Trips are public and unauthenticated by design for the prototype: anyone with the link can
read or edit. Creation refuses to clobber an existing slug; replacement is an explicit PUT.

The 501 endpoints are deliberate. Their schemas are fully defined so they appear in the
OpenAPI document and generate TypeScript types, letting the frontend build against the real
shapes before the backend implementation lands.
"""

import logging
from collections.abc import AsyncIterator, Sequence

from fastapi import APIRouter, Response, status
from fastapi.responses import StreamingResponse

from motorooter.api.deps import Blurbs, ChatModel, Discovery, Lookup, Resolver, Trips
from motorooter.api.errors import NotImplementedYet
from motorooter.api.schemas import (
    ERROR_RESPONSES,
    ChatEvent,
    ChatRequest,
    CreateTripRequest,
    ErrorResponse,
    ReplanEvent,
    ReplanRequest,
    RouteThroughBestRequest,
    RouteThroughBestResponse,
    TripBlurbRequest,
    TripBlurbResponse,
    UpdateTripRequest,
)
from motorooter.blurb.models import Turn
from motorooter.chat.prompt import CHAT_SYSTEM_PROMPT
from motorooter.chat.routing import LegRoutingService
from motorooter.chat.tools import TripTools
from motorooter.gpx import trip_to_gpx
from motorooter.llm.agent import Agent
from motorooter.llm.messages import AssistantMessage, Message, SystemMessage, UserMessage
from motorooter.planning.discovery.corridor import SearchCorridor
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.planning.route_through import route_through_best
from motorooter.planning.stitching import search_corridor
from motorooter.routing.errors import RouteIncomplete
from motorooter.trips.models import PoiCategory, Trip, TripSummary, utc_now
from motorooter.trips.service import edit_trip
from motorooter.trips.slug import slugify, validate_slug

router = APIRouter(prefix="/api/trips", tags=["trips"], responses=ERROR_RESPONSES)

NOT_IMPLEMENTED = status.HTTP_501_NOT_IMPLEMENTED

logger = logging.getLogger(__name__)


GPX_MEDIA_TYPE = "application/gpx+xml"

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
        default_intent=request.default_intent,
    )


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(slug: str, store: Trips) -> None:
    await store.delete(validate_slug(slug))


@router.post(
    "/{slug}/route-through-best",
    response_model=RouteThroughBestResponse,
    summary="Reroute through the best places discovery found",
)
async def route_through_best_endpoint(
    slug: str, request: RouteThroughBestRequest, store: Trips, resolver: Resolver
) -> RouteThroughBestResponse:
    """Add the best of the trip's saved places to its route, as via-points.

    The mouse's half of a capability the assistant also has, over the same service function.
    A checkbox on the Replan button would have made this a chat feature with an affordance
    bolted on, and it would only have been reachable during a sixty-second search — the
    scores are on the trip, so this needs no search at all.

    Fast path: no LLM, no metered discovery, one routing request to confirm the new order
    joins before anything is written.
    """
    result = await route_through_best(
        store=store,
        slug=validate_slug(slug),
        router=LegRoutingService(resolver),
        limit=request.limit,
    )
    return RouteThroughBestResponse(
        trip=result.trip, added=list(result.added), left_out=list(result.left_out)
    )


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

    corridor = search_corridor(trip)
    if corridor is None:
        raise RouteIncomplete(trip.unrouted_leg_indices or (0,))

    return StreamingResponse(
        _stream(discovery, corridor, request.categories),
        media_type=STREAMING_MEDIA_TYPE,
    )


async def _stream(
    discovery: DiscoveryPipeline,
    corridor: SearchCorridor,
    categories: Sequence[PoiCategory],
) -> AsyncIterator[bytes]:
    """One `ReplanEvent` per line.

    Errors after the first byte cannot change the status code, so an unexpected failure is
    emitted as a final event rather than allowed to truncate the stream. A client that sees
    the connection close mid-line cannot tell a crash from a network drop; one that reads
    `stage: done` with a failure message can.
    """
    try:
        async for step in discovery.run(corridor, list(categories)):
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
    summary="Talk to the assistant about a trip",
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
        NOT_IMPLEMENTED: {
            "model": ErrorResponse,
            "description": "No chat model configured on this deployment.",
        },
    },
)
async def chat(
    slug: str,
    request: ChatRequest,
    store: Trips,
    resolver: Resolver,
    discovery: Discovery,
    model: ChatModel,
    lookup: Lookup,
) -> StreamingResponse:
    """Run one assistant turn against a trip, streaming what happens.

    The trip is read before streaming starts so an unknown slug is a 404 with a body, rather
    than a 200 whose first line says something went wrong. After the first byte the status
    code is fixed, which is why everything downstream of here is reported as an event.
    """
    slug = validate_slug(slug)
    trip = await store.get(slug)
    if model is None:
        raise NotImplementedYet("chat (no chat model configured)")

    tools = TripTools(
        store=store,
        slug=slug,
        router=LegRoutingService(resolver),
        discovery=discovery,
        lookup=lookup,
    )
    agent = Agent(model, tools.registry)
    return StreamingResponse(
        _chat_stream(agent, tools, request, trip.name),
        media_type=STREAMING_MEDIA_TYPE,
    )


async def _chat_stream(
    agent: Agent,
    tools: TripTools,
    request: ChatRequest,
    trip_name: str,
) -> AsyncIterator[bytes]:
    """Agent events as `ChatEvent` lines.

    `trip_changed` marks the event that moved the document, and the terminal `done` if
    anything moved during the turn. It answers two questions and they are not the same one:
    "re-read now" and "did anything change this turn", the second being what a client reading
    only the tail depends on.

    It used to be sticky on every event after the first change, which made the first answer
    wrong. The rule lived here, where no client can read it, while the schema description said
    "the assistant edited the trip ... re-read it" — a statement about the event in hand. A
    client that did exactly that re-read the document once per remaining event in the turn.
    Both halves are in the schema description now, because that is the one a client sees.

    An unexpected failure becomes a final `done` rather than a truncated connection. A client
    seeing the stream stop mid-line cannot distinguish a crash from a dropped network.
    """
    changed = False
    try:
        async for step in agent.run(_conversation(request, trip_name)):
            # This event's own answer, not the turn's. `done` gets the accumulator below.
            moved = bool(step.outcome and step.outcome.payload.get("trip_changed"))
            changed = changed or moved
            event = ChatEvent(
                kind=step.kind,
                message=step.message,
                tool=step.tool,
                progress=step.progress,
                truncated=step.truncated,
                trip_changed=changed if step.kind == "done" else moved,
            )
            yield event.model_dump_json().encode() + b"\n"
    except Exception:
        logger.exception("chat stream failed")
        failed = ChatEvent(
            kind="done",
            message="the assistant stopped after an unexpected error",
            truncated=True,
            trip_changed=changed,
        )
        yield failed.model_dump_json().encode() + b"\n"


def _conversation(request: ChatRequest, trip_name: str) -> list[Message]:
    """The system prompt, the client's transcript, then this turn.

    The client owns the history, so "what did the assistant see" is answerable from the
    request alone and the server stays stateless across turns.
    """
    messages: list[Message] = [SystemMessage(content=CHAT_SYSTEM_PROMPT.format(trip=trip_name))]
    for turn in request.history:
        messages.append(
            AssistantMessage(content=turn.content)
            if turn.role == "assistant"
            else UserMessage(content=turn.content)
        )
    messages.append(UserMessage(content=request.message))
    return messages


@router.post(
    "/{slug}/blurb",
    response_model=TripBlurbResponse,
    summary="One line about this trip, for the rail header",
    description=(
        "A short, casual line characterising the trip the rider is looking at — the chat "
        "rail's header, in place of static copy.\n\n"
        "**Decoration, not information.** `blurb` is null whenever no usable line was "
        "produced, which is not an error: treat null and 501 identically and keep whatever "
        "header you were showing. Nothing else in the app should wait on this call.\n\n"
        "**Not a chat feature.** `history` is optional and the trip document is the input, "
        "so a rider who has never opened the rail still gets a line. Every figure the model "
        "may use is measured from the trip and handed to it; it is forbidden to state a "
        "number or a place it was not given. Answers 501 when no OpenAI key is configured."
    ),
    responses={NOT_IMPLEMENTED: {"model": ErrorResponse}},
)
async def trip_blurb(
    slug: str, request: TripBlurbRequest, store: Trips, blurbs: Blurbs
) -> TripBlurbResponse:
    """The rail header's line for this trip.

    A missing trip is still a 404 rather than a null blurb: the client asked about something
    that does not exist, which is a different answer from "nothing to say about this one".
    """
    trip = await store.get(validate_slug(slug))
    if blurbs is None:
        raise NotImplementedYet("trip blurbs (no OpenAI credentials configured)")

    history = tuple(Turn(role=turn.role, content=turn.content) for turn in request.history)
    return TripBlurbResponse(blurb=await blurbs.write(trip, history))


@router.get(
    "/{slug}/gpx",
    summary="Export GPX",
    description=(
        "A GPX 1.1 file containing one track — one segment per leg — plus ordered "
        "waypoints, targeted at motorcycle GPS units. Discovered POIs travel as waypoints "
        "carrying their category, which on the device is most of the value.\n\n"
        "Long routes are **decimated, never truncated**: the whole route is kept and the "
        "point budget is spent on the corners, because a truncated track hands a rider the "
        "first part of their day with nothing to say the rest is missing."
    ),
    response_class=Response,
    responses={200: {"description": "GPX file.", "content": {GPX_MEDIA_TYPE: {}}}},
)
async def export_gpx(slug: str, store: Trips) -> Response:
    """The trip as a GPX file.

    No parameters: the trip document is already what the rider curated, and a second filter
    here would be a second thing to keep in step with it. A trip with no routed geometry
    still exports — its waypoints are worth having, and refusing would be a worse answer
    than a file with no track in it.
    """
    trip = await store.get(validate_slug(slug))
    return Response(content=trip_to_gpx(trip), media_type=GPX_MEDIA_TYPE)
