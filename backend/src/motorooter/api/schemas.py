"""Request and response schemas.

This module *is* the frontend contract. Every type here becomes a TypeScript interface via
OpenAPI generation, so changes ripple straight into the frontend build — treat edits as
breaking-change decisions, not refactors.

Domain models (`routing.models`, `trips.models`) are reused directly where they are already
the right shape. Wrapper types exist only where the API needs to say something the domain
model does not, such as the drag-throttle budget on a routed leg.
"""

from pydantic import BaseModel, ConfigDict, Field

from motorooter.routing.models import Coordinate, LegIntent, ProviderCapabilities, RouteLeg
from motorooter.trips.models import Poi, PoiCategory, PoiDetail, TripLeg, Waypoint


class ErrorResponse(BaseModel):
    """Uniform error body. `code` is stable and machine-readable; `detail` is for humans."""

    code: str
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
    preserve_pinned: bool = Field(
        default=True,
        description=(
            "Keep POIs the user pinned and legs they have not touched, rather than "
            "discarding the whole plan."
        ),
    )


class ReplanEventKind(BaseModel):
    """Documentation-only marker for the SSE event vocabulary.

    Replan streams Server-Sent Events so the map fills in incrementally instead of
    freezing behind a spinner. Event `data` is a JSON `ReplanEvent`.
    """


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


class PoiDetailResponse(BaseModel):
    """Places-backed display data for the POI dialog.

    Never cached server-side beyond `place_id`; Google's terms do not permit it.
    """

    detail: PoiDetail
