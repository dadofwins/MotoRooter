"""Short-circuits a span whose waypoints are all the same place.

A loop trip reaches this state on its way to being built: "three days of dirt starting and
ending in Leavenworth" makes the trip briefly `[Leavenworth, Leavenworth]`, and a leg whose
two ends are one coordinate is a legitimate question with a degenerate answer. Asking an
engine costs a metered request to be told what the coordinates already say, and hosted ORS
answers it with a single point — which `RouteLeg.geometry` rightly refuses, since stitching,
surface-span indexing and `geometry_length_m` all assume a leg has two ends.

A decorator rather than a check in a service, because there are three entry points that
route a span — the chat tools, trip routing, and the drag fast path — and each resolves its
provider through `PolicyResolver`. Guarding one of them diverges the paths; guarding the
provider they all share means no caller can miss it.

This does not replace the adapters' own degenerate-reply guards, and neither covers the
other: this handles identical *input*, they handle a degenerate *reply*, which an engine can
also return for distinct points that snap to the same node.
"""

from motorooter.routing.geo import haversine_m
from motorooter.routing.models import ProviderCapabilities, RouteLeg, RouteRequest
from motorooter.routing.protocol import RoutingProvider

COINCIDENT_SPAN_TOLERANCE_M = 1.0
"""Below this, two waypoints are the same place. Absorbs float and rounding jitter.

Deliberately the same value as `planning.stitching.COINCIDENT_TOLERANCE_M`, and deliberately
not imported from it: routing does not depend on planning, and inverting that to share a
float would be the more expensive mistake. They mean the same thing at two layers — a
boundary this close is one point — so a span this short is one stitching would collapse
anyway.
"""


class CoincidentSpanProvider:
    """Answers a zero-length span itself instead of paying an engine to.

    The leg it returns is honestly two coincident points: it draws as nothing, contributes
    nothing to distance, and stitching already collapses boundaries this close. Nothing is
    reported to the rider, because a loop mid-construction is an ordinary transient state
    and warning about one would fire during normal use.
    """

    def __init__(self, inner: RoutingProvider) -> None:
        self._inner = inner

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    async def route(self, request: RouteRequest) -> RouteLeg:
        if not self._is_coincident(request):
            return await self._inner.route(request)

        start = request.waypoints[0]
        return RouteLeg(
            geometry=(start, start),
            distance_m=0.0,
            duration_s=0.0,
            surface_spans=(),
            ascent_m=None,
            provider=self.capabilities.name,
            intent=request.intent,
            # Mirrors the engine this stood in for, so the leg is indistinguishable from one
            # it produced. Zero seconds is right either way — the derived estimate over zero
            # distance is also zero — but a stamp that disagreed with the provider tag would
            # be a difference downstream has no way to explain.
            duration_is_trustworthy=self.capabilities.reports_trustworthy_duration,
        )

    @staticmethod
    def _is_coincident(request: RouteRequest) -> bool:
        """True when every waypoint is the first one. A via-point makes it a real route."""
        start = request.waypoints[0]
        return all(
            haversine_m(start, point) <= COINCIDENT_SPAN_TOLERANCE_M
            for point in request.waypoints[1:]
        )
