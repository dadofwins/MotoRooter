"""Routing, as the tools need it.

A tool wants "can these points be joined, and what does the road look like" and nothing else.
Handing it the whole registry would let a tool reach a provider by name, which is the one
thing the routing architecture forbids — so it gets the policy resolver and a single method.
"""

from motorooter.routing.models import LegIntent, RouteLeg, RouteRequest, stamped
from motorooter.routing.policy import PolicyResolver
from motorooter.trips.models import Waypoint


class LegRoutingService:
    """Routes a span of waypoints through whichever engine the intent resolves to."""

    def __init__(self, resolver: PolicyResolver) -> None:
        self._resolver = resolver

    async def route_waypoints(
        self,
        waypoints: tuple[Waypoint, ...],
        *,
        intent: LegIntent,
        provider_override: str | None = None,
    ) -> tuple[RouteLeg, ...]:
        """One leg spanning every waypoint given.

        Raises:
            RoutingError: the points cannot be joined. This is the check on model-invented
                geography — a coordinate in the sea fails here rather than becoming a pin —
                so callers translate it into something the model can act on rather than
                letting it escape as a 500.
        """
        provider = self._resolver.resolve(intent, override=provider_override)
        request = RouteRequest(
            waypoints=tuple(point.coordinate for point in waypoints), intent=intent
        )
        leg = stamped(await provider.route(request), request, provider_override=provider_override)
        return (leg,)
