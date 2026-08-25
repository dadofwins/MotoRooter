"""Joining routed legs into one continuous geometry.

A trip is a list of legs, each routed by whichever engine its policy resolved to. Rendering
the route, exporting GPX, and reporting how much of the trip is dirt all need those legs as
a single polyline — and the joins are where this goes wrong.

Two problems, and the whole module is about them.

**Endpoint mismatch.** Adjacent legs share a waypoint, but each engine snaps that waypoint
to the nearest node *it* knows about, and two engines routinely pick different nodes. Three
regimes, rather than one threshold pretending to be enough:

- within `coincident_tolerance_m`: the same point, seen twice. Emit one vertex.
- beyond that but within `gap_threshold_m`: ordinary snapping disagreement. Keep both
  vertices — the polyline bridges them — and say nothing.
- beyond `gap_threshold_m`: something is wrong with the route. Keep both vertices and
  record a `LegGap`, so the UI can show it and the rider is not silently handed a
  teleport.

Nothing is ever invented to close a hole. The bridge is the straight segment a renderer
would draw anyway; what this module adds is knowing the hole is there.

**Index drift.** A `SurfaceSpan` addresses positions in its own leg's geometry. Stitching
moves all of them, and by a different amount depending on whether the boundary vertex was
deduplicated. Getting it wrong corrupts the dirt statistic while the map still looks
perfect, which is the worst kind of bug this code can have — so the offset is computed once
per leg, at the point the leg's vertices are appended, and never recomputed.

Spans are shifted, not merged. Two abutting spans of the same surface across a boundary
stay two spans: merging is cosmetic, and the arithmetic that makes it safe is exactly the
arithmetic most likely to introduce the drift this module exists to avoid.
"""

from collections.abc import Sequence
from functools import cached_property
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from motorooter.routing.geo import haversine_m, path_length_m
from motorooter.routing.models import Coordinate, RouteLeg, Surface, SurfaceSpan

COINCIDENT_TOLERANCE_M = 1.0
"""Below this, two boundary vertices are the same point. Absorbs float and rounding jitter."""

GAP_REPORT_THRESHOLD_M = 25.0
"""Above this, a boundary mismatch is worth telling someone about.

Engines snapping to different nodes on the same road disagree by a few metres constantly;
warning about that would be noise. Twenty-five metres is more than a lane and less than a
missed turn.
"""


class LegGap(BaseModel):
    """A boundary where two adjacent legs do not meet.

    Carries both endpoints so the UI can zoom to the hole rather than just report a number.
    """

    model_config = ConfigDict(frozen=True)

    after_leg_index: int = Field(ge=0, description="The gap follows the leg at this index.")
    distance_m: float = Field(ge=0.0)
    end: Coordinate
    """Last vertex of the earlier leg."""

    start: Coordinate
    """First vertex of the later leg."""


class StitchedRoute(BaseModel):
    """Routed legs joined into one polyline, with the joins accounted for."""

    model_config = ConfigDict(frozen=True)

    geometry: tuple[Coordinate, ...] = ()
    surface_spans: tuple[SurfaceSpan, ...] = ()
    """Reindexed into `geometry`. Per-leg, so abutting spans of one surface are not merged."""

    legs: tuple[RouteLeg, ...] = ()
    gaps: tuple[LegGap, ...] = ()

    leg_start_indices: tuple[int, ...] = ()
    """Where each leg begins in `geometry`, so a caller can map a vertex back to its leg."""

    @model_validator(mode="after")
    def _spans_within_geometry(self) -> Self:
        """The reindexing invariant, asserted on every constructed route."""
        limit = len(self.geometry) - 1
        for span in self.surface_spans:
            if span.end_index > limit:
                msg = (
                    f"stitched surface span end_index {span.end_index} exceeds geometry "
                    f"index {limit}; leg offsets are wrong"
                )
                raise ValueError(msg)
        return self

    @property
    def is_continuous(self) -> bool:
        """Whether every boundary met within tolerance."""
        return not self.gaps

    @property
    def distance_m(self) -> float:
        """Summed provider-reported distance.

        Excludes bridged gaps, so this agrees with `Trip.total_distance_m` for the same
        trip. `bridged_distance_m` reports the fabricated remainder separately.
        """
        return sum(leg.distance_m for leg in self.legs)

    @property
    def bridged_distance_m(self) -> float:
        """Distance spanned by straight-line bridges across reported gaps."""
        return sum(gap.distance_m for gap in self.gaps)

    @property
    def duration_s(self) -> float:
        return sum(leg.duration_s for leg in self.legs)

    @cached_property
    def geometry_length_m(self) -> float:
        """Measured length of the joined polyline, bridges included."""
        return path_length_m(self.geometry)

    @cached_property
    def unpaved_distance_m(self) -> float:
        """Metres explicitly tagged unpaved.

        Measured from the stitched geometry via the reindexed spans, so a reindexing bug
        shows up here as a wrong number rather than hiding behind the per-leg totals.
        """
        return sum(
            path_length_m(self.geometry[span.start_index : span.end_index + 1])
            for span in self.surface_spans
            if span.surface is Surface.UNPAVED
        )

    @property
    def unpaved_fraction(self) -> float:
        """Share of the whole route on dirt, weighted by distance.

        Both sides are measured from geometry. Averaging per-leg fractions would let a
        short dirt connector beside a long highway read as half the trip.
        """
        total = self.geometry_length_m
        return self.unpaved_distance_m / total if total > 0 else 0.0


def stitch(
    legs: Sequence[RouteLeg],
    *,
    coincident_tolerance_m: float = COINCIDENT_TOLERANCE_M,
    gap_threshold_m: float = GAP_REPORT_THRESHOLD_M,
) -> StitchedRoute:
    """Join routed legs, in order, into one continuous geometry.

    Args:
        legs: routed legs in travel order. An empty sequence yields an empty route, which
            is the correct answer for a trip nobody has routed yet.
        coincident_tolerance_m: below this, two boundary vertices are one point.
        gap_threshold_m: above this, a boundary mismatch is recorded as a `LegGap`.

    Raises:
        ValueError: the thresholds are inconsistent.
    """
    if gap_threshold_m < coincident_tolerance_m:
        msg = (
            f"gap_threshold_m {gap_threshold_m} is below coincident_tolerance_m "
            f"{coincident_tolerance_m}; a boundary would be merged and reported as a gap "
            "at the same time"
        )
        raise ValueError(msg)

    geometry: list[Coordinate] = []
    spans: list[SurfaceSpan] = []
    gaps: list[LegGap] = []
    starts: list[int] = []

    for index, leg in enumerate(legs):
        vertices = leg.geometry
        if not geometry:
            offset = 0
        else:
            previous_end = geometry[-1]
            separation = haversine_m(previous_end, vertices[0])
            if separation <= coincident_tolerance_m:
                # The shared waypoint, seen twice. The leg's local index 0 refers to the
                # vertex already in `geometry`, so the offset lands one short of the end.
                offset = len(geometry) - 1
                vertices = vertices[1:]
            else:
                offset = len(geometry)
                if separation > gap_threshold_m:
                    gaps.append(
                        LegGap(
                            after_leg_index=index - 1,
                            distance_m=separation,
                            end=previous_end,
                            start=vertices[0],
                        )
                    )

        starts.append(offset)
        geometry.extend(vertices)
        spans.extend(
            span.model_copy(
                update={
                    "start_index": span.start_index + offset,
                    "end_index": span.end_index + offset,
                }
            )
            for span in leg.surface_spans
        )

    return StitchedRoute(
        geometry=tuple(geometry),
        surface_spans=tuple(spans),
        legs=tuple(legs),
        gaps=tuple(gaps),
        leg_start_indices=tuple(starts),
    )
