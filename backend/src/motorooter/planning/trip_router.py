"""Routing a trip's legs through the engines their policies resolve to.

Pipeline stage 2, minus the LLM. Every leg carries its own intent, so which engine handles
which section is a property of the data and never a branch here — this module asks the
resolver and does as it is told.

**Partial failure is a result, not an exception.** A ten-leg trip where one dirt section is
unroutable still has nine legs' worth of geometry, and on a free tier metered at ~2,000
requests a day those nine responses are quota already spent. Raising would discard both.
`route_trip` therefore returns successes and failures side by side, which is also the shape
replan needs: `ReplanEvent` streams legs as they land, and a stage that can only report
total success or total failure cannot stream anything useful.

The one thing that *does* raise is stitching a trip whose geometry is not both present and
current. Two ways that goes wrong, and the second is the subtle one:

- a leg was never routed, so the stitched route is silently short;
- a leg's re-route *failed* and it kept its previous geometry. Keeping it is deliberate —
  losing a good route to a failed retry is a downgrade nobody asked for — but stitching it
  is not. Retag a paved leg as dirt, have the dirt engine time out, and the export renders
  perfectly at 0% unpaved while carrying the paved road you replaced.

Every routed leg records the request it came from (`RouteLeg.routed_from`), so staleness is
decided by comparing requests rather than guessed at by comparing geometry. A dragged
waypoint, a retag, an added via point, and a repinned provider all change the fingerprint;
engine snapping does not.

`stitch_result` remains the stronger path. A fingerprint says the inputs are unchanged, not
that the last attempt succeeded — a leg re-routed for identical inputs by an engine that
then 503s keeps geometry that is genuinely current, and only the result knows the attempt
failed at all.
"""

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from motorooter.api.error_codes import resolve
from motorooter.error_codes import ErrorCode
from motorooter.planning.stitching import (
    GAP_REPORT_THRESHOLD_M,
    StitchedRoute,
    stitch,
)
from motorooter.routing.errors import RouteIncomplete, RoutingError
from motorooter.routing.geo import COINCIDENT_TOLERANCE_M
from motorooter.routing.models import (
    RouteLeg,
    RouteRequest,
    stamped,
)
from motorooter.routing.policy import PolicyResolver
from motorooter.trips.models import Trip, TripLeg


class LegRoutingFailure(BaseModel):
    """Why one leg could not be routed."""

    model_config = ConfigDict(frozen=True)

    leg_index: int = Field(ge=0)
    code: ErrorCode
    """The same identifier the HTTP layer would send for this failure.

    The enum rather than a bare string: this value is copied onto `TripLeg` and serialized
    into the trip document, and a raw string there silently produces a field that is not
    actually an `ErrorCode` — equal to one, but not one.
    """

    detail: str
    retryable: bool = False
    """Whether trying again could plausibly succeed. Quota exhaustion is not retryable."""


class TripRoutingResult(BaseModel):
    """A routed trip plus whatever refused to route."""

    model_config = ConfigDict(frozen=True)

    trip: Trip
    failures: tuple[LegRoutingFailure, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.failures


def _describe(index: int, exc: Exception) -> "LegRoutingFailure":
    """Turn a leg's exception into a reportable failure.

    The code comes from the same `ERROR_TABLE` the HTTP layer uses, so a failure listed here
    and the same failure raised from an endpoint carry the identical identifier. An
    untranslated adapter error resolves to `internal_error` rather than inventing a code the
    client has never seen.
    """
    _, code = resolve(exc)
    return LegRoutingFailure(
        leg_index=index,
        code=code,
        detail=str(exc),
        retryable=isinstance(exc, RoutingError) and exc.retryable,
    )


class TripRouter:
    """Routes trip legs and joins the results."""

    def __init__(self, resolver: PolicyResolver) -> None:
        self._resolver = resolver

    async def route_trip(self, trip: Trip, *, only_unrouted: bool = False) -> TripRoutingResult:
        """Route every leg, concurrently.

        Args:
            trip: the trip to route. Not mutated; a new one is returned.
            only_unrouted: skip legs that already have geometry. This is what makes replan
                incremental — a leg the user did not touch keeps the route it had, and
                costs no quota to keep.

        Legs are routed concurrently because they are independent. That does not increase
        the request count, only how quickly it is spent.
        """
        targets = [
            index
            for index, leg in enumerate(trip.legs)
            if not (only_unrouted and leg.routed is not None)
        ]
        outcomes = await asyncio.gather(
            *(self._safely_route(trip, index) for index in targets),
            return_exceptions=True,
        )
        return self._assemble(trip, dict(zip(targets, outcomes, strict=True)))

    async def route_leg(self, trip: Trip, leg_index: int) -> TripRoutingResult:
        """Re-route a single leg, leaving every other leg exactly as it was.

        The fast path. Dragging the route re-requests the affected leg only; a whole-route
        recompute is what makes an editor feel sluggish, and here it would also multiply
        the quota cost of one gesture by the number of legs.

        Raises:
            IndexError: no leg at that index.
        """
        # Negative indices are rejected rather than wrapped. Python would happily route
        # `legs[-1]`, but `_assemble` matches outcomes by position, so the result would be
        # written to a slot nothing reads: quota spent, trip unchanged, no error raised.
        if not 0 <= leg_index < len(trip.legs):
            msg = f"trip has {len(trip.legs)} legs; no leg at index {leg_index}"
            raise IndexError(msg)

        # Indexed outside `_safely_route`'s try block, so a bad index surfaces as the
        # caller bug it is rather than as a leg the engine refused.
        outcome = await self._safely_route(trip, leg_index)
        return self._assemble(trip, {leg_index: outcome})

    def stitch_result(
        self,
        result: TripRoutingResult,
        *,
        coincident_tolerance_m: float = COINCIDENT_TOLERANCE_M,
        gap_threshold_m: float = GAP_REPORT_THRESHOLD_M,
    ) -> StitchedRoute:
        """Join a routing result, refusing anything that did not fully succeed.

        The safe entry point. `is_complete` is a property a caller can forget to read; this
        cannot be forgotten, because the refusal is the return path.

        Raises:
            RouteIncomplete: any leg failed to route, whatever geometry it may have kept.
        """
        if not result.is_complete:
            raise RouteIncomplete(tuple(f.leg_index for f in result.failures))
        return self.stitch_trip(
            result.trip,
            coincident_tolerance_m=coincident_tolerance_m,
            gap_threshold_m=gap_threshold_m,
        )

    def stitch_trip(
        self,
        trip: Trip,
        *,
        allow_stale: bool = False,
        coincident_tolerance_m: float = COINCIDENT_TOLERANCE_M,
        gap_threshold_m: float = GAP_REPORT_THRESHOLD_M,
    ) -> StitchedRoute:
        """Join the trip's routed legs into one continuous geometry.

        For a trip loaded from storage, where there is no `TripRoutingResult` to check
        against. Prefer `stitch_result` when you have one.

        Args:
            trip: a fully and currently routed trip.
            allow_stale: stitch geometry that no longer matches its leg. For showing a user
                what they currently have; never for an export.
            coincident_tolerance_m: below this, two boundary vertices are one point.
            gap_threshold_m: above this, a boundary mismatch is recorded as a `LegGap`.

        Raises:
            RouteIncomplete: a leg has no geometry, or has geometry produced under a
                different intent or provider than it now carries. Refused rather than
                skipped: a route missing or misrepresenting a section still looks whole.
        """
        missing = tuple(index for index, leg in enumerate(trip.legs) if leg.routed is None)
        if missing:
            raise RouteIncomplete(missing)
        if not allow_stale:
            stale = self._stale_leg_indices(trip)
            if stale:
                raise RouteIncomplete(stale, reason="stale")
        return stitch(
            [leg.routed for leg in trip.legs if leg.routed is not None],
            coincident_tolerance_m=coincident_tolerance_m,
            gap_threshold_m=gap_threshold_m,
        )

    def _stale_leg_indices(self, trip: Trip) -> tuple[int, ...]:
        """Legs whose geometry came from a request the leg no longer describes.

        The judgement itself is `TripLeg.has_current_geometry`, shared with the rebuild that
        decides which legs keep their geometry after a waypoint edit. Two answers to "is this
        route what the rider is looking at" would be two answers to the only question that
        matters here.

        A leg with no geometry is not stale — it is unrouted, which the caller handles
        separately — so it is skipped rather than reported.
        """
        return tuple(
            index
            for index, leg in enumerate(trip.legs)
            if leg.routed is not None
            and not leg.has_current_geometry(
                trip.waypoints[leg.start_waypoint_index : leg.end_waypoint_index + 1]
            )
        )

    def leg_request(self, trip: Trip, leg: TripLeg) -> RouteRequest:
        """The routing request for one leg: its own waypoint span, and nothing else.

        Public so the LLM tool layer can route trip legs through this exact call rather
        than assembling its own request — two paths building requests differently is how
        the mouse path and the chat path start behaving differently.
        """
        waypoints = tuple(
            waypoint.coordinate
            for waypoint in trip.waypoints[leg.start_waypoint_index : leg.end_waypoint_index + 1]
        )
        return RouteRequest(waypoints=waypoints, intent=leg.intent)

    async def _safely_route(self, trip: Trip, index: int) -> RouteLeg | Exception:
        """Route one leg, returning the failure rather than raising it.

        Returned, not raised, so one leg's failure cannot abandon the legs routing alongside
        it — their responses are quota already spent.

        Catches `Exception`, not just `RoutingError`. An adapter that leaks an untranslated
        error is a bug, but discarding nine paid-for legs is not the way to report it: the
        failure is recorded against its leg with an `internal_error` code, and the rest of
        the trip survives. `BaseException` is deliberately not caught, so cancellation still
        cancels rather than being filed as "this leg did not route".
        """
        leg = trip.legs[index]
        request = self.leg_request(trip, leg)
        try:
            provider = self._resolver.resolve(leg.intent, override=leg.provider_override)
            routed = await provider.route(request)
        except Exception as exc:  # noqa: BLE001 -- deliberate; see docstring
            return exc
        # Stamped here rather than in each adapter: what is stamped describes the request,
        # which this layer owns, and asking every provider to attach it would be duplicated
        # and easy to forget in a new adapter.
        #
        # Through `stamped` rather than by hand. This site built its own `model_copy` and so
        # was the one place a new stamped field would be silently omitted — which is the
        # exact failure `stamped` was written for, one field earlier.
        return stamped(
            routed,
            request,
            provider_override=leg.provider_override,
            duration_is_trustworthy=provider.capabilities.reports_trustworthy_duration,
        )

    def _assemble(
        self, trip: Trip, outcomes: dict[int, RouteLeg | BaseException]
    ) -> TripRoutingResult:
        """Fold per-leg outcomes back into a trip, keeping prior geometry where routing failed."""
        legs: list[TripLeg] = []
        failures: list[LegRoutingFailure] = []

        for index, leg in enumerate(trip.legs):
            outcome = outcomes.get(index)
            if outcome is None:
                legs.append(leg)  # Skipped, e.g. only_unrouted.
            elif isinstance(outcome, RouteLeg):
                # Clearing the marker matters as much as setting it: a leg that has just
                # routed successfully must not stay flagged as broken.
                legs.append(leg.model_copy(update={"routed": outcome, "last_routing_error": None}))
            elif isinstance(outcome, Exception):
                # The prior geometry stays. Losing a good route because a re-route failed
                # would be a downgrade the user never asked for — and `stitch_trip` refuses
                # to export geometry that no longer matches its leg, so it cannot mislead.
                failure = _describe(index, outcome)
                # Recorded on the leg as well as in `failures`: only the leg survives being
                # saved, and `failures` is not part of the trip the store accepts.
                legs.append(leg.model_copy(update={"last_routing_error": failure.code}))
                failures.append(failure)
            else:
                # Not an Exception at all — a BaseException that escaped gather. Cancellation
                # and interrupts belong to the caller, not in a failure list.
                raise outcome

        return TripRoutingResult(
            trip=trip.model_copy(update={"legs": tuple(legs)}),
            failures=tuple(failures),
        )
