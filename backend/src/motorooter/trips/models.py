"""Trip and POI domain models.

These cross every boundary in the system — API, Cloud Storage, and (via generated
TypeScript) the frontend — so their invariants are enforced here rather than restated in
each layer.

Two decisions are load-bearing and easy to undo by accident:

- `Poi` holds only `place_id` from Google Places. Ratings, photos, and reviews live on
  `PoiDetail`, which is response-only and never persisted, because Google's terms permit
  indefinite storage of `place_id` and very little else. A field that exists on the
  persisted model will eventually be written to the bucket.
- `needs_replan` is derived from timestamps, not stored. A stored boolean drifts.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from motorooter.error_codes import ErrorCode
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    RouteFingerprint,
    RouteLeg,
    RouteRequest,
    Surface,
)
from motorooter.speeds import (
    DEFAULT_RIDING_SPEEDS,
    RidingSpeeds,
    leg_duration_s,
)

MINIMUM_SPAN = 2
"""Below two waypoints there is no leg to have geometry for."""

CURRENT_SCHEMA_VERSION = 1

DEFAULT_INTENT = LegIntent.UNPAVED
"""How a leg routes when nothing has said otherwise.

**The only unstated default in the system.** There were three, and they disagreed: the chat
tools invented `twisty_paved` in two places and the frontend invented `unpaved` in a third,
on the explicit grounds that dirt is the point of an adventure motorcycle planner. Same
question, two answers, on either side of the contract — and the frontend had the better of
the argument, so this is its value rather than the tools'.

Everything else reads `Trip.default_intent`, and this is what that falls back to when a trip
has never had a mode stated. Adding a fourth constant is the failure to avoid; if a caller
needs a default, it needs this one.
"""


class PoiCategory(StrEnum):
    """What a point of interest is, which drives its map icon and discovery prompt."""

    WILD_CAMP = "wild_camp"
    CAMPGROUND = "campground"
    HOTEL = "hotel"
    UNIQUE_STAY = "unique_stay"
    FOOD = "food"
    FUEL = "fuel"
    WATER = "water"
    VIEWPOINT = "viewpoint"
    MECHANIC = "mechanic"


class PoiSource(StrEnum):
    """Where a POI came from, which determines how far to trust its coordinates."""

    LLM_SUGGESTED = "llm_suggested"
    """Model output. Unverified: coordinates may be invented until resolved via Places."""

    PLACES = "places"
    """Resolved against Google Places. Has a real `place_id`."""

    USER = "user"
    """Placed by the user on the map. Trusted by definition."""


class Poi(BaseModel):
    """A point of interest, as persisted in the trip document."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    category: PoiCategory
    coordinate: Coordinate
    source: PoiSource

    place_id: str | None = None
    """Google Places identifier — the only Places field safe to store indefinitely."""

    on_route: bool = False
    """Pinned into the route by the user. Requires verification; see below."""

    note: str | None = None
    """Why this is here, in the judge's own words when discovery found it.

    Never a quoted review and never a numeric rating — that would be caching Places content
    through a side door. The judge is instructed accordingly; see `judge.py`.
    """

    score: float | None = Field(default=None, ge=0.0, le=1.0)
    """What the judge made of it, or `None` if nothing judged it.

    Absent rather than zero: a pin the rider dropped themselves was never scored, and
    defaulting it to zero would rank it below every place the judge disliked.

    Storable where `rating` is not, because it is a number we computed rather than a Places
    field. It is kept so that routing through the best of them stops being welded to a
    sixty-second search — a rider can change their mind an hour later without paying for
    discovery twice.

    Worth knowing before trusting it as an absolute: judging the same corridor twice moves
    individual scores by up to 0.15, so the ordering is meaningful and the exact value is
    not. Rank by it; do not draw fine distinctions with it.
    """

    @property
    def is_verified(self) -> bool:
        """Whether this POI's location can be trusted.

        Model suggestions are only trustworthy once resolved against Places; anything the
        user placed themselves is trusted as-is.
        """
        return self.source is not PoiSource.LLM_SUGGESTED or self.place_id is not None

    @model_validator(mode="after")
    def _only_verified_pois_reach_the_route(self) -> Self:
        if self.on_route and not self.is_verified:
            msg = (
                f"POI {self.name!r} is unverified (LLM-suggested with no resolved place_id) "
                "and cannot be added to the route"
            )
            raise ValueError(msg)
        return self


class PoiDetail(BaseModel):
    """Display data for the POI dialog. Response-only — never written to storage.

    Everything beyond `poi` is re-fetched from Places on demand, because caching it would
    breach Google's terms.
    """

    model_config = ConfigDict(frozen=True)

    poi: Poi
    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    user_rating_count: int | None = Field(default=None, ge=0)
    photo_urls: tuple[str, ...] = ()
    reviews: tuple[str, ...] = ()
    website: str | None = None
    phone: str | None = None
    opening_hours: tuple[str, ...] = ()


class Waypoint(BaseModel):
    """An anchor the route must pass through."""

    model_config = ConfigDict(frozen=True)

    coordinate: Coordinate
    name: str | None = None

    pinned: bool = True
    """True for a user-placed anchor; False for a shaping point inserted by a drag."""


class TripLeg(BaseModel):
    """One section of the route, spanning a pair of waypoints.

    Each leg carries its own intent and optional provider override, which is what makes
    per-road-type routing a property of the data rather than a branch in routing code.
    """

    model_config = ConfigDict(frozen=True)

    intent: LegIntent
    start_waypoint_index: int = Field(ge=0)
    end_waypoint_index: int = Field(gt=0)

    provider_override: str | None = None
    """Pin this section to a named engine, overriding the policy table."""

    routed: RouteLeg | None = None
    """Cached geometry from the last successful route. `None` until first routed."""

    last_routing_error: ErrorCode | None = None
    """Why the most recent routing attempt failed, or `None` if none has.

    Without this, a leg with no geometry is byte-identical whether its routing failed or was
    never attempted — the distinction lived only in `TripRoutingResult`, which is not part of
    the trip and so does not survive being saved. A rider whose dirt leg timed out can still
    save and retry later; the trip records that the section is broken rather than unplanned.

    Cleared by a successful route. A stale marker would leave a healthy leg flagged forever.
    """

    def has_current_geometry(self, span: Sequence["Waypoint"]) -> bool:
        """Whether `routed` still describes the leg this now is, given its waypoints.

        **Compares the request, not the endpoints.** An engine snaps to the nearest routable
        node, sometimes by hundreds of metres, so there is no tolerance that separates
        snapping from a rider dragging the point. `RouteFingerprint` records what was asked
        for, and that has no such ambiguity.

        Two callers, and they ask for opposite reasons. The exporter asks so it can refuse a
        trip whose geometry no longer matches — a route missing or misrepresenting a section
        renders perfectly, so nothing downstream would catch it. A rebuild asks so it can
        *keep* geometry that is still good, instead of discarding every leg because one
        waypoint moved and making the rider wait for a route they already had.

        A leg with no fingerprint falls back to comparing the intent, which is weaker: it
        cannot see a moved waypoint. Those are documents written before the field existed,
        and refusing them would turn a missing annotation into a broken trip.
        """
        routed = self.routed
        if routed is None or len(span) < MINIMUM_SPAN:
            return False
        if routed.routed_from is None:
            return routed.intent is self.intent
        return routed.routed_from == RouteFingerprint.of(
            RouteRequest(waypoints=tuple(point.coordinate for point in span), intent=self.intent),
            provider_override=self.provider_override,
        )

    @model_validator(mode="after")
    def _must_move_forward(self) -> Self:
        if self.end_waypoint_index <= self.start_waypoint_index:
            msg = (
                f"leg end_waypoint_index {self.end_waypoint_index} must exceed "
                f"start_waypoint_index {self.start_waypoint_index}"
            )
            raise ValueError(msg)
        return self


class Trip(BaseModel):
    """A saved trip. Serialized verbatim to `trips/<slug>/trip.json`."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = CURRENT_SCHEMA_VERSION
    slug: str
    name: str

    created_at: AwareDatetime
    edited_at: AwareDatetime
    """Last change to geometry or waypoints. Drives the replan staleness check."""

    planned_at: AwareDatetime | None = None
    """Last completed replan, or `None` if never planned."""

    waypoints: tuple[Waypoint, ...] = ()
    legs: tuple[TripLeg, ...] = ()
    pois: tuple[Poi, ...] = ()

    default_intent: LegIntent | None = None
    """What kind of trip the rider said this is. Seeds new legs; decides nothing on its own.

    `TripLeg.intent` is still what routes each section — this only says what a *new* section
    should start as. `None` means nobody stated a preference, which is the ordinary case for
    a trip built with the mouse.

    It exists because the mode was previously remembered only by legs that happened to
    exist. A trip stripped back to one waypoint has no legs, so rebuilding it fell through to
    a hardcoded paved default and a rider who had asked for as much dirt as possible got a
    paved route back with nothing to indicate anything had been forgotten.
    """

    @model_validator(mode="after")
    def _legs_reference_real_contiguous_waypoints(self) -> Self:
        limit = len(self.waypoints) - 1
        for leg in self.legs:
            if leg.end_waypoint_index > limit:
                msg = (
                    f"leg references waypoint index {leg.end_waypoint_index} but the trip "
                    f"has {len(self.waypoints)} waypoints"
                )
                raise ValueError(msg)

        # A gap between consecutive legs means the exported route teleports.
        for previous, following in zip(self.legs, self.legs[1:], strict=False):
            if following.start_waypoint_index != previous.end_waypoint_index:
                msg = (
                    "legs must be contiguous: leg starting at waypoint "
                    f"{following.start_waypoint_index} does not continue from the previous "
                    f"leg ending at {previous.end_waypoint_index}"
                )
                raise ValueError(msg)
        return self

    @property
    def intent_for_new_legs(self) -> LegIntent:
        """How a leg added to this trip should route.

        One rule, deliberately: the mode the rider stated, or the product's default. The
        alternative — inheriting from whichever leg happens to be first — is what the missing
        field was standing in for, and it has a cliff in it. A trip stripped back below two
        waypoints has no legs to inherit from, so a rider who asked for as much dirt as
        possible got a paved rebuild with nothing to indicate anything had been forgotten.
        """
        return self.default_intent or DEFAULT_INTENT

    @property
    def is_fully_routed(self) -> bool:
        """Whether every leg has geometry.

        A property rather than a field, so it cannot drift and does not change the wire
        shape. `total_distance_m` sums only the legs that routed, so a partial trip reports a
        smaller number with nothing to mark it — this is what makes that number
        interpretable.
        """
        return all(leg.routed is not None for leg in self.legs)

    @property
    def unrouted_leg_indices(self) -> tuple[int, ...]:
        """Legs with no geometry, in order. Empty when the trip is fully routed."""
        return tuple(index for index, leg in enumerate(self.legs) if leg.routed is None)

    @property
    def needs_replan(self) -> bool:
        """Whether discovery results are stale relative to the current geometry.

        Derived rather than stored so it cannot drift. Surfaced on the Replan button —
        stale suggestions the user cannot detect are worse than no suggestions.
        """
        return self.planned_at is None or self.edited_at > self.planned_at

    @property
    def routed_legs(self) -> tuple[RouteLeg, ...]:
        return tuple(leg.routed for leg in self.legs if leg.routed is not None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_distance_m(self) -> float:
        return sum(leg.distance_m for leg in self.routed_legs)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_paved_fraction(self) -> float:
        """Share of the trip on surfaced road, weighted by distance."""
        return self._surface_fraction(Surface.PAVED)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_unpaved_fraction(self) -> float:
        """Share of the whole trip on dirt, weighted by leg distance.

        Both sides of the ratio are measured from geometry. Dividing geometry-derived
        unpaved metres by the provider's reported `distance_m` mixes two denominators that
        need not agree, and the error compounds across legs. `total_distance_m` still
        reports the provider's figure, which is the better number to *display*.

        Weighting by distance also matters: averaging per-leg fractions would let a short
        dirt connector beside a long highway read as half the trip.
        """
        return self._surface_fraction(Surface.UNPAVED)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_unknown_fraction(self) -> float:
        """Share of the trip whose surface nobody has recorded.

        Reported as its own number so the UI can show three states. Folded into either of
        the others it disappears, and it disappears into whichever the client treats as the
        default — which is how an unsurveyed forest road comes to be displayed as tarmac.
        """
        return self._surface_fraction(Surface.UNKNOWN)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estimated_duration_s(self) -> float:
        """Riding time, taking the best available figure for each leg.

        Per leg, not per trip. Hosted ORS routes dirt through a bicycle profile and reports
        bicycle times — 8 hours for 133 km — so its figure is useless and the speed table
        wins. Google runs a car profile, and on 177 km of highway its figure beats the table
        by half an hour. Applying either rule to the whole trip gets the other half wrong,
        and the trusted-everywhere direction is the dangerous one: it would time a technical
        unpaved section as though it were pavement.
        """
        return self.estimate_duration_s()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_is_estimated(self) -> bool:
        """Whether any part of the total was derived rather than reported.

        True unless every routed leg carried a trustworthy duration, and true for a trip
        with no geometry at all — claiming exactness for a total of zero is worse than
        admitting it is a guess. A number that looks exact when half of it is derived is the
        failure this exists to prevent.
        """
        legs = self.routed_legs
        return not legs or not all(leg.duration_is_trustworthy for leg in legs)

    def estimate_duration_s(self, speeds: RidingSpeeds = DEFAULT_RIDING_SPEEDS) -> float:
        """Riding time under a given speed table, so the table can be varied in tests.

        The table only applies to legs whose provider is not trusted on duration; a trusted
        leg contributes what its engine said, and no speed table overrides it.
        """
        return sum(leg_duration_s(leg, speeds) for leg in self.routed_legs)

    def _surface_fraction(self, surface: Surface) -> float:
        measured = sum(leg.geometry_length_m for leg in self.routed_legs)
        if measured <= 0:
            return 0.0
        return self._surface_distance_m(surface) / measured

    def _surface_distance_m(self, surface: Surface) -> float:
        match surface:
            case Surface.PAVED:
                return sum(leg.paved_distance_m for leg in self.routed_legs)
            case Surface.UNPAVED:
                return sum(leg.unpaved_distance_m for leg in self.routed_legs)
            case Surface.UNKNOWN:
                return sum(leg.unknown_distance_m for leg in self.routed_legs)


TripName = Annotated[str, Field(min_length=1, max_length=200)]
"""A human-entered trip name, before slugification."""


class TripSummary(BaseModel):
    """Listing entry. Avoids shipping full geometry for every trip in the index."""

    model_config = ConfigDict(frozen=True)

    slug: str
    name: str
    created_at: AwareDatetime
    edited_at: AwareDatetime
    total_distance_m: float

    total_paved_fraction: float = 0.0
    total_unpaved_fraction: float = 0.0
    total_unknown_fraction: float = 0.0
    """The same three shares the trip reports, so the index and the trip cannot disagree."""

    estimated_duration_s: float = 0.0
    """The best figure available per leg. See `Trip.estimated_duration_s`.

    Was "derived, never the provider's duration", which stopped being true when duration
    trustworthiness became a capability rather than a global rule.
    """

    duration_is_estimated: bool = True
    """Whether any part of that total was derived rather than reported by its engine.

    Carried into the summary so the trip list cannot show a figure the trip page would
    caveat. Defaults true: an unset flag should read as a guess.
    """

    needs_replan: bool

    @classmethod
    def from_trip(cls, trip: Trip) -> Self:
        """Copied from the trip rather than recomputed, so there is one derivation."""
        return cls(
            slug=trip.slug,
            name=trip.name,
            created_at=trip.created_at,
            edited_at=trip.edited_at,
            total_distance_m=trip.total_distance_m,
            total_paved_fraction=trip.total_paved_fraction,
            total_unpaved_fraction=trip.total_unpaved_fraction,
            total_unknown_fraction=trip.total_unknown_fraction,
            estimated_duration_s=trip.estimated_duration_s,
            duration_is_estimated=trip.duration_is_estimated,
            needs_replan=trip.needs_replan,
        )


def utc_now() -> datetime:
    """Injectable-friendly clock for trip timestamps.

    A module-level function rather than a `default_factory` so tests can monkeypatch it
    and so timestamps never appear implicitly in a model default.
    """
    from datetime import UTC

    return datetime.now(UTC)
