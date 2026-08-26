"""Request and response schemas.

This module *is* the frontend contract. Every type here becomes a TypeScript interface via
OpenAPI generation, so changes ripple straight into the frontend build — treat edits as
breaking-change decisions, not refactors.

Domain models (`routing.models`, `trips.models`) are reused directly where they are already
the right shape. Wrapper types exist only where the API needs to say something the domain
model does not, such as the drag-throttle budget on a routed leg.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from motorooter.api.error_codes import ErrorCode
from motorooter.routing.models import Coordinate, LegIntent, ProviderCapabilities, RouteLeg
from motorooter.trips.models import Poi, PoiCategory, PoiDetail, Trip, TripLeg, Waypoint


class ErrorResponse(BaseModel):
    """Uniform error body. `code` is stable and machine-readable; `detail` is for humans."""

    code: ErrorCode = Field(
        description=(
            "Stable, machine-readable error identifier. Switch on this, never on `detail`. "
            "Typed as an enum so the frontend generates the union instead of "
            "hand-maintaining it."
        )
    )
    detail: str


ERROR_RESPONSES: dict[int | str, dict[str, type[ErrorResponse]]] = {
    status: {"model": ErrorResponse} for status in (400, 404, 409, 422, 429, 502, 503)
}
"""Attached to every router so error bodies appear in the OpenAPI document.

Without this the generated TypeScript has no `ErrorResponse` type and the frontend ends up
hand-writing the error shape — which then silently drifts from the real one.
"""


class HealthResponse(BaseModel):
    status: str
    providers: list[str]


class IntentRouting(BaseModel):
    """How one leg intent is currently routed, and how fast the UI may re-request it."""

    provider: str
    live_update_interval_ms: int | None = Field(
        default=None,
        description=(
            "Minimum milliseconds between live re-routes while dragging. null means "
            "preview-only: rubber-band locally and route only on release."
        ),
    )
    reports_trustworthy_duration: bool = Field(
        default=False,
        description=(
            "Whether this intent's engine returns a duration worth showing. False means "
            "the figure shown is derived from distance and surface, not reported: hosted "
            "ORS routes dirt through a bicycle profile and its times are bicycle times. "
            "Resolved here rather than client-side, for the same reason as the throttle — "
            "a hand-kept intent-to-engine map goes stale the day the policy table moves."
        ),
    )


class RoutingCapabilitiesResponse(BaseModel):
    """Lets the frontend throttle per provider without hardcoding an engine name."""

    providers: list[ProviderCapabilities]
    intents: dict[str, IntentRouting]


class RouteLegRequest(BaseModel):
    """Route one leg. The fast path — no LLM involvement, no persistence."""

    model_config = ConfigDict(frozen=True)

    waypoints: list[Coordinate] = Field(min_length=2)
    intent: LegIntent
    provider_override: str | None = Field(
        default=None,
        description="Pin this leg to a named engine, bypassing the policy table.",
    )
    avoid_highways: bool = False
    avoid_tolls: bool = False
    avoid_ferries: bool = False
    want_elevation: bool = False


class RouteLegResponse(BaseModel):
    leg: RouteLeg
    live_update_interval_ms: int | None = Field(
        default=None,
        description="Throttle budget for the engine that served this leg.",
    )
    estimated_duration_s: float = Field(
        description=(
            "Riding time derived from distance and surface, in seconds. Use this, not "
            "`leg.duration_s` — hosted ORS routes dirt through a bicycle profile and "
            "reports bicycle times, measured at 8 hours for 133 km. Computed server-side "
            "so the speed table has one home rather than being reimplemented per client."
        )
    )


class CreateTripRequest(BaseModel):
    """Create a trip. Everything is public and unauthenticated for now."""

    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(
        default=None,
        description="Explicit slug. Omit to derive one from the name.",
    )


class UpdateTripRequest(BaseModel):
    """Replace a trip's editable content.

    A full replacement rather than a patch: the frontend holds the authoritative trip
    state while editing, and partial updates would need conflict rules that an
    unauthenticated prototype cannot enforce anyway.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    waypoints: list[Waypoint] | None = None
    legs: list[TripLeg] | None = None
    pois: list[Poi] | None = None
    default_intent: LegIntent | None = Field(
        default=None,
        description=(
            "What kind of trip this is. Seeds any leg added later, on either the map or the "
            "chat path; each leg's own intent still decides how it routes. Send it when the "
            "rider picks a mode for the whole trip rather than for one section."
        ),
    )


class RouteThroughBestRequest(BaseModel):
    """Reroute through the best of the places already found. Fast, and searches nothing."""

    limit: int | None = Field(
        default=None,
        ge=0,
        le=20,
        description=(
            "How many places at most. Omit for the pace the ride implies — roughly one stop "
            "every two hours of riding. Zero adds nothing, which is a legitimate way to ask "
            "what would be chosen without choosing it."
        ),
    )


class RouteThroughBestResponse(BaseModel):
    """What the reroute did, in enough detail to show the rider and let them undo it."""

    trip: Trip = Field(description="The saved trip, so the map can redraw without re-reading.")
    added: list[Poi] = Field(
        description=(
            "Places now on the route, in the order they will be ridden. Each carries the "
            "reason it was chosen in `note` — a route that changed for reasons the rider "
            "cannot see is worse than a route that did not change."
        )
    )
    left_out: list[Poi] = Field(
        description=(
            "Places good enough to add that the count or the detour budget had no room for. "
            "Offer them; a bound the rider cannot see reads as having found nothing."
        )
    )


class ReplanRequest(BaseModel):
    """Start the slow path. Explicitly user-triggered; never fired by a route edit."""

    categories: list[PoiCategory] = Field(
        default_factory=lambda: list(PoiCategory),
        description="Which kinds of POI to discover along the route.",
    )
    prompt: str | None = Field(
        default=None,
        description="Free-text steer from the chat bar, e.g. 'prefer hot springs'.",
    )


class ReplanEvent(BaseModel):
    """One streamed progress event from a replan."""

    stage: str = Field(description="One of: route_search, discovery, enrichment, done.")
    message: str
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    pois: list[Poi] = Field(
        default_factory=list,
        description="POIs discovered so far in this stage. Cumulative per stage, not total.",
    )
    legs: list[TripLeg] = Field(
        default_factory=list,
        description="Legs re-routed by this stage, if any.",
    )


class ChatRequest(BaseModel):
    """One turn of conversation about a trip.

    The trip is addressed by slug rather than sent, so the assistant reads and edits the
    same document the mouse does. There is no conversation id: the client sends the history
    it wants considered, which keeps the server stateless and makes "what did the assistant
    see" answerable from the request alone.
    """

    message: str = Field(min_length=1, max_length=4000)
    history: list["ChatTurn"] = Field(
        default_factory=list,
        description="Prior turns, oldest first. The client owns the transcript.",
    )


class ChatTurn(BaseModel):
    """A previous exchange, as the client recorded it."""

    role: Literal["user", "assistant"]
    content: str


class ChatEvent(BaseModel):
    """One streamed step of an assistant turn.

    Mirrors what the agent loop already emits, so the transport adds no vocabulary of its
    own. Streamed as newline-delimited JSON, one event per line — same framing as replan,
    which the client already parses.
    """

    kind: Literal[
        "message",
        "tool_started",
        "tool_progress",
        "tool_finished",
        "tool_failed",
        "done",
    ] = Field(description="What happened. `done` is always last.")
    message: str = Field(
        default="",
        description="Assistant text for `message`, or a human-readable note for tool events.",
    )
    tool: str | None = Field(
        default=None, description="Which tool, on tool events. Null otherwise."
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Set on the terminal `done` event when a limit stopped the run. On the terminal "
            "event specifically, because a client reading only the last event must be able "
            "to tell 'finished' from 'cut off mid-task'."
        ),
    )
    progress: float | None = Field(
        default=None,
        description=(
            "On a `tool_progress` event, how far through **that tool** the run is, 0 to 1. "
            "Null on every other kind, and null when a tool cannot say — read it as "
            "unknown rather than as zero.\n\n"
            "Within the tool, not within the turn: a turn's total work is decided by the "
            "model while it is deciding it, so a turn-level fraction would be invented. "
            "`tool` names which tool it belongs to, so a turn that runs two of them can be "
            "followed without inferring from ordering."
        ),
        ge=0.0,
        le=1.0,
    )
    trip_changed: bool = Field(
        default=False,
        description=(
            "The assistant edited the trip. The client should re-read it rather than "
            "reconstruct the change from the event stream — the mouse path and the chat "
            "path must converge on one document, not two models of it."
        ),
    )


class GeocodeResult(BaseModel):
    """One place a search text resolved to, verified against Google Places."""

    name: str = Field(description="What Places calls it, not what was typed.")
    place_id: str = Field(
        description=(
            "Google's identifier. The only field here that may be stored, and the way to "
            "refer to this place later without re-resolving it."
        )
    )
    address: str | None = Field(
        default=None,
        description=(
            "Places' formatted address, e.g. 'Leavenworth, WA 98826, USA'. **This is what "
            "makes a list of same-named places choosable** — without it three real "
            "Leavenworths render as three identical rows. Null when Places has none, which "
            "a natural feature often will not; fall back to `kinds` there. Display only, "
            "per-request, never stored."
        ),
    )
    coordinate: Coordinate
    kinds: list[str] = Field(
        default_factory=list,
        description=(
            "Places' own types, e.g. `locality`, `natural_feature`. For choosing an icon "
            "and for telling a town from a mountain pass when names collide."
        ),
    )


class GeocodeResponse(BaseModel):
    """Places matching a search text, best first.

    **Several results, not one.** A name is a claim until something verifies it, and plenty of
    names verify to more than one real place — Leavenworth is a town in Washington, a town in
    Kansas and a district of Bavaria. Returning the list and letting the caller choose keeps
    the choice with whoever has the context, rather than resolving an ambiguity silently and
    plausibly.

    Empty when nothing matched. That is an ordinary answer, not an error.
    """

    results: list[GeocodeResult] = Field(
        description="Best first. Empty if the text matched nothing."
    )


class PoiDetailResponse(BaseModel):
    """Places-backed display data for the POI dialog.

    Never cached server-side beyond `place_id`; Google's terms do not permit it.
    """

    detail: PoiDetail
