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

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from motorooter.routing.models import Coordinate, LegIntent, RouteLeg

CURRENT_SCHEMA_VERSION = 1


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
    def needs_replan(self) -> bool:
        """Whether discovery results are stale relative to the current geometry.

        Derived rather than stored so it cannot drift. Surfaced on the Replan button —
        stale suggestions the user cannot detect are worse than no suggestions.
        """
        return self.planned_at is None or self.edited_at > self.planned_at

    @property
    def routed_legs(self) -> tuple[RouteLeg, ...]:
        return tuple(leg.routed for leg in self.legs if leg.routed is not None)

    @property
    def total_distance_m(self) -> float:
        return sum(leg.distance_m for leg in self.routed_legs)

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
        total = sum(leg.geometry_length_m for leg in self.routed_legs)
        if total <= 0:
            return 0.0
        unpaved = sum(leg.unpaved_distance_m for leg in self.routed_legs)
        return unpaved / total


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
    needs_replan: bool

    @classmethod
    def from_trip(cls, trip: Trip) -> Self:
        return cls(
            slug=trip.slug,
            name=trip.name,
            created_at=trip.created_at,
            edited_at=trip.edited_at,
            total_distance_m=trip.total_distance_m,
            needs_replan=trip.needs_replan,
        )


def utc_now() -> datetime:
    """Injectable-friendly clock for trip timestamps.

    A module-level function rather than a `default_factory` so tests can monkeypatch it
    and so timestamps never appear implicitly in a model default.
    """
    from datetime import UTC

    return datetime.now(UTC)
