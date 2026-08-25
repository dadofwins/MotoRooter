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

The one thing that *does* raise is stitching a partially-routed trip. There, silence would
produce a shorter route that looks whole.
"""

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from motorooter.planning.stitching import StitchedRoute, stitch
from motorooter.routing.errors import RouteIncomplete, RoutingError
from motorooter.routing.models import RouteLeg, RouteRequest
from motorooter.routing.policy import PolicyResolver
from motorooter.trips.models import Trip, TripLeg


class LegRoutingFailure(BaseModel):
    """Why one leg could not be routed."""

    model_config = ConfigDict(frozen=True)

    leg_index: int = Field(ge=0)
    code: str
    """snake_case error identifier, matching the API's error envelope vocabulary."""

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


def _error_code(exc: Exception) -> str:
    """snake_case identifier from the class name, matching `api.exception_handlers`."""
    name = type(exc).__name__
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


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

    def stitch_trip(self, trip: Trip, **options: float) -> StitchedRoute:
        """Join the trip's routed legs into one continuous geometry.

        Args:
            trip: a fully routed trip.
            **options: forwarded to `stitch` — `coincident_tolerance_m`, `gap_threshold_m`.

        Raises:
            RouteIncomplete: some leg has no geometry. Refused rather than skipped: a
                stitched route missing a section is indistinguishable from a shorter trip.
        """
        missing = tuple(index for index, leg in enumerate(trip.legs) if leg.routed is None)
        if missing:
            raise RouteIncomplete(missing)
        return stitch([leg.routed for leg in trip.legs if leg.routed is not None], **options)

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

    async def _safely_route(self, trip: Trip, index: int) -> RouteLeg | RoutingError:
        """Route one leg, returning the failure rather than raising it.

        Returned, not raised, so one leg's failure cannot abandon the legs routing
        alongside it — their responses are quota already spent.
        """
        leg = trip.legs[index]
        try:
            provider = self._resolver.resolve(leg.intent, override=leg.provider_override)
            return await provider.route(self.leg_request(trip, leg))
        except RoutingError as exc:
            return exc

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
                legs.append(leg.model_copy(update={"routed": outcome}))
            elif isinstance(outcome, RoutingError):
                # The prior geometry stays. Losing a good route because a re-route failed
                # would be a downgrade the user never asked for.
                legs.append(leg)
                failures.append(
                    LegRoutingFailure(
                        leg_index=index,
                        code=_error_code(outcome),
                        detail=str(outcome),
                        retryable=outcome.retryable,
                    )
                )
            else:
                # Not a routing failure — a bug here or in an adapter. Do not swallow it.
                raise outcome

        return TripRoutingResult(
            trip=trip.model_copy(update={"legs": tuple(legs)}),
            failures=tuple(failures),
        )
