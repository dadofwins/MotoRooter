"""Provider-neutral routing domain models.

Every adapter normalizes into these types; nothing provider-shaped is allowed to escape
an adapter module. These models also generate the TypeScript types consumed by the
frontend, so their invariants are the contract for both sides.
"""

from enum import StrEnum
from functools import cached_property
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from motorooter.routing.geo import path_length_m


class Coordinate(BaseModel):
    """A WGS84 point.

    Frozen and hashable so requests built from coordinates can be used as cache keys.
    """

    model_config = ConfigDict(frozen=True)

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)

    def to_geojson(self) -> tuple[float, float]:
        """GeoJSON position order: (lon, lat). Transposing this is the classic map bug."""
        return (self.lon, self.lat)

    @classmethod
    def from_geojson(cls, position: tuple[float, float]) -> Self:
        lon, lat = position
        return cls(lat=lat, lon=lon)


COORDINATE_KEY_PRECISION = 5
"""Decimal places a coordinate is rounded to when used for identity rather than position.

About 1.1 m. Enough to absorb float jitter between two runs of the same drag, tight enough
that genuinely different waypoints stay distinct. Shared by the routing cache's key and by
`RouteFingerprint`: if the two disagreed, a cache hit could be reported as stale geometry,
or a request the cache would re-fetch could reuse a leg.
"""


class Surface(StrEnum):
    PAVED = "paved"
    UNPAVED = "unpaved"
    UNKNOWN = "unknown"
    """Explicitly distinct from UNPAVED: absence of data is not evidence of dirt."""


class LegIntent(StrEnum):
    """What a leg is *for*. The policy resolver maps this to a provider and profile.

    This is the unit of "different routing algorithm per road type" — it is a property
    of trip data, not a branch in routing code.
    """

    HIGHWAY_CONNECTOR = "highway_connector"
    TWISTY_PAVED = "twisty_paved"
    UNPAVED = "unpaved"
    TECHNICAL_OFFROAD = "technical_offroad"
    MANUAL_TRACK = "manual_track"
    """User-drawn; routed as-is with no engine snapping."""


class SurfaceSpan(BaseModel):
    """A run of geometry sharing one surface type, as inclusive indices into `geometry`."""

    model_config = ConfigDict(frozen=True)

    start_index: int = Field(ge=0)
    end_index: int = Field(gt=0)
    surface: Surface

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        if self.end_index <= self.start_index:
            msg = f"end_index {self.end_index} must exceed start_index {self.start_index}"
            raise ValueError(msg)
        return self


class RouteRequest(BaseModel):
    """A request for one leg. Frozen so it is safe to use as a cache key."""

    model_config = ConfigDict(frozen=True)

    waypoints: tuple[Coordinate, ...] = Field(min_length=2)
    intent: LegIntent
    avoid_highways: bool = False
    avoid_tolls: bool = False
    avoid_ferries: bool = False
    want_elevation: bool = False


class RouteFingerprint(BaseModel):
    """The request a leg's geometry was produced from.

    Recorded so staleness is decidable rather than guessed. Comparing a cached leg's
    endpoints against its waypoints cannot work — engines snap to the nearest routable node,
    sometimes by hundreds of metres, so there is no tolerance that separates snapping from a
    user dragging the point. Comparing the *request* has no such ambiguity.

    Stores rounded coordinates rather than a hash. A hash would be smaller and sufficient for
    equality, but when a rider reports a route wrongly marked stale, a hash says nothing and
    these values name the field that moved.
    """

    model_config = ConfigDict(frozen=True)

    waypoints: tuple[Coordinate, ...] = Field(min_length=2)
    """Rounded to `COORDINATE_KEY_PRECISION`, matching the routing cache's key."""

    intent: LegIntent
    provider_override: str | None = None

    @classmethod
    def of(cls, request: RouteRequest, *, provider_override: str | None = None) -> Self:
        return cls(
            waypoints=tuple(
                Coordinate(
                    lat=round(waypoint.lat, COORDINATE_KEY_PRECISION),
                    lon=round(waypoint.lon, COORDINATE_KEY_PRECISION),
                )
                for waypoint in request.waypoints
            ),
            intent=request.intent,
            provider_override=provider_override,
        )


class RouteLeg(BaseModel):
    """A routed leg, normalized across providers."""

    model_config = ConfigDict(frozen=True)

    geometry: tuple[Coordinate, ...] = Field(min_length=2)
    distance_m: float = Field(ge=0.0)
    duration_s: float = Field(ge=0.0)
    surface_spans: tuple[SurfaceSpan, ...] = ()
    ascent_m: float | None = None
    provider: str
    intent: LegIntent

    routed_from: RouteFingerprint | None = None
    """The request this geometry came from, when one was recorded.

    `None` for a leg routed outside a trip — the single-leg fast-path endpoint does not
    persist anything, so it has nothing to go stale against.
    """

    @model_validator(mode="after")
    def _spans_within_geometry(self) -> Self:
        limit = len(self.geometry) - 1
        for span in self.surface_spans:
            if span.end_index > limit:
                msg = f"surface span end_index {span.end_index} exceeds geometry index {limit}"
                raise ValueError(msg)
        return self

    @cached_property
    def geometry_length_m(self) -> float:
        """Length of the geometry itself, independent of the provider's reported distance."""
        return path_length_m(self.geometry)

    @cached_property
    def unpaved_distance_m(self) -> float:
        """Metres explicitly tagged unpaved. UNKNOWN spans do not count."""
        return sum(
            path_length_m(self.geometry[span.start_index : span.end_index + 1])
            for span in self.surface_spans
            if span.surface is Surface.UNPAVED
        )

    @cached_property
    def unpaved_fraction(self) -> float:
        """Share of the leg on dirt, in [0, 1]. No surface data reads as 0.0."""
        total = self.geometry_length_m
        return self.unpaved_distance_m / total if total > 0 else 0.0


class ProviderCapabilities(BaseModel):
    """What a provider can do and what it costs.

    The policy resolver dispatches on these fields rather than on provider names, so
    adding an engine never touches dispatch logic.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    prefers_unpaved: bool = False
    """Can actively weight routing *toward* dirt, not merely tolerate it."""

    map_matching: bool = False
    alternatives: bool = False
    elevation: bool = False
    max_waypoints: int = Field(default=50, ge=2)

    live_update_interval_ms: int | None = Field(default=None, ge=0)
    """Minimum gap between live re-routes during a drag.

    `None` means preview-only: the UI rubber-bands a straight line during the gesture and
    issues no request until release. Use for providers whose quota is the binding
    constraint.
    """

    daily_quota: int | None = Field(default=None, ge=0)
    """Requests per day, if the provider imposes one. `None` means effectively unlimited."""

    @property
    def supports_live_updates(self) -> bool:
        return self.live_update_interval_ms is not None
