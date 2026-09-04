"""Provider-neutral routing domain models.

Every adapter normalizes into these types; nothing provider-shaped is allowed to escape
an adapter module. These models also generate the TypeScript types consumed by the
frontend, so their invariants are the contract for both sides.
"""

from collections.abc import Mapping
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


RIDER_FACING_MODE: Mapping[LegIntent, str] = {
    LegIntent.HIGHWAY_CONNECTOR: "Fast",
    LegIntent.TWISTY_PAVED: "Twisties",
    LegIntent.UNPAVED: "Offroad",
}
"""The three modes a rider is shown, keyed by the intent that expresses them.

`technical_offroad` and `manual_track` are deliberately absent rather than mapped to a
near-neighbour: the field expresses five intents and only three have been named, so a
missing key is the honest answer for the other two and a caller decides what to do about it.

Here because there was no canonical mapping anywhere — the labels existed only inside tool
descriptions, as prose, in three places. Anything else needing them should read this rather
than write a fourth copy that goes stale when the vocabulary changes.
"""


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

    duration_is_trustworthy: bool = False
    """Whether `duration_s` is worth showing a rider, stamped when the leg was routed.

    On the leg rather than looked up, so a trip saved last week still knows: re-resolving
    the policy table would answer for whichever engine the intent points at *now*, and that
    table has been repointed before. Same argument as `routed_from`.

    Defaults false, which is the safe direction — a leg that arrived without the stamp gets
    the derived estimate rather than a bicycle time presented as fact.
    """

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
        return self._distance_tagged(Surface.UNPAVED)

    @cached_property
    def paved_distance_m(self) -> float:
        """Metres explicitly tagged paved."""
        return self._distance_tagged(Surface.PAVED)

    @cached_property
    def unknown_distance_m(self) -> float:
        """Metres this leg cannot vouch for.

        Everything the spans do not positively identify as paved or unpaved: explicit
        UNKNOWN spans, and geometry no span covers at all. Defined as the remainder rather
        than summed from UNKNOWN spans, because untagged geometry is exactly as unknown as
        geometry tagged unknown — and folding it into either of the other two is how a road
        nobody has surveyed comes to be reported as tarmac.
        """
        accounted = self.paved_distance_m + self.unpaved_distance_m
        return max(self.geometry_length_m - accounted, 0.0)

    def _distance_tagged(self, surface: Surface) -> float:
        return sum(
            path_length_m(self.geometry[span.start_index : span.end_index + 1])
            for span in self.surface_spans
            if span.surface is surface
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

    reports_surface: bool = False
    """Whether this engine reports what the road is made of.

    Distinct from `prefers_unpaved`, which is about what it will route *onto*. Google wants
    false for both and means different things by each: it will route you down a gravel road,
    it just will not say so.

    Defaults to false so an engine has to claim it — assuming otherwise would make an
    engine's silence indistinguishable from a genuine "surface unknown", which is the
    conflation this flag exists to remove. A client can then say "this engine does not report
    surface" instead of drawing an unexplained grey line.
    """

    reports_trustworthy_duration: bool = False
    """Whether this engine's own duration is worth showing a rider.

    Distinct from `reports_surface`, and the two go opposite ways: hosted ORS reports surface
    and cannot be trusted on time, because the only profile that reaches dirt is a bicycle
    one — 8 hours for 133 km. Google reports no surface and runs a car profile, so on
    177 km of highway its figure beats our speed table by half an hour.

    Defaults false so an engine has to claim it. M0 concluded "provider durations are
    unusable, compute our own" from the ORS measurement and it was written down as a global
    rule; it is a property of the profile a provider ran, which is why it lives here and is
    resolved per intent rather than branched on an engine name.
    """

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

    per_minute_quota: int | None = Field(default=None, ge=0)
    """Requests per minute, if the provider imposes one.

    A separate limit with a separate window, not a fraction of the daily one. Enforcing only
    the daily cap lets a burst sail past the local guard and come back as an opaque upstream
    failure — which is what a discovery fan-out looks like.
    """

    @property
    def supports_live_updates(self) -> bool:
        return self.live_update_interval_ms is not None


def stamped(
    leg: "RouteLeg",
    request: "RouteRequest",
    *,
    provider_override: str | None,
    duration_is_trustworthy: bool = False,
) -> "RouteLeg":
    """A leg carrying what it needs to be understood later, without asking anything.

    One helper rather than two call sites. `routed_from` shipped attached on the trip path
    and absent on the single-leg one, so every drag response had a null fingerprint, the
    client correctly read that as stale, and each drag routed twice. The field being optional
    is what let it go unnoticed — nothing complains about a null that is allowed.

    `duration_is_trustworthy` is stamped here for the same reason and to avoid the same bug:
    two places that must remember to attach it is one place too many.

    Anything that turns a `RouteRequest` into a `RouteLeg` should call this.
    """
    return leg.model_copy(
        update={
            "routed_from": RouteFingerprint.of(request, provider_override=provider_override),
            "duration_is_trustworthy": duration_is_trustworthy,
        }
    )
