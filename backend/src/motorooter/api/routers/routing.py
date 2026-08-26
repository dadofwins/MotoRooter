"""Routing endpoints — the fast path.

Nothing here touches an LLM or persists anything. These are the calls the drag interaction
makes, so latency is the design constraint: one leg per request, never a whole-route
recompute.
"""

from fastapi import APIRouter

from motorooter.api.deps import Registry, Resolver
from motorooter.api.schemas import (
    ERROR_RESPONSES,
    IntentRouting,
    RouteLegRequest,
    RouteLegResponse,
    RoutingCapabilitiesResponse,
)
from motorooter.routing.models import RouteRequest, stamped
from motorooter.speeds import estimate_leg_duration_s

router = APIRouter(prefix="/api/routing", tags=["routing"], responses=ERROR_RESPONSES)


@router.get("/capabilities", response_model=RoutingCapabilitiesResponse)
async def capabilities(registry: Registry, resolver: Resolver) -> RoutingCapabilitiesResponse:
    """What each engine supports, and the drag-throttle budget per leg intent.

    The frontend reads `live_update_interval_ms` from here rather than hardcoding a
    per-engine constant, so retuning throttles is a backend config change.
    """
    return RoutingCapabilitiesResponse(
        providers=[p.capabilities for p in registry],
        intents={
            intent.value: IntentRouting(
                provider=resolver.resolve(intent).capabilities.name,
                live_update_interval_ms=resolver.live_update_interval_ms(intent),
                reports_trustworthy_duration=(
                    resolver.resolve(intent).capabilities.reports_trustworthy_duration
                ),
            )
            for intent in resolver.configured_intents()
        },
    )


@router.post("/leg", response_model=RouteLegResponse)
async def route_leg(request: RouteLegRequest, resolver: Resolver) -> RouteLegResponse:
    """Route a single leg.

    Called on every throttled drag update and again on release, so it must stay cheap.
    Routing errors are translated to HTTP by the registered exception handlers.
    """
    provider = resolver.resolve(request.intent, override=request.provider_override)
    route_request = RouteRequest(
        waypoints=tuple(request.waypoints),
        intent=request.intent,
        avoid_highways=request.avoid_highways,
        avoid_tolls=request.avoid_tolls,
        avoid_ferries=request.avoid_ferries,
        want_elevation=request.want_elevation,
    )
    # Stamped with the request it came from, so the client can tell a stale leg from a
    # current one. Without it every response looks stale and every drag routes twice.
    leg = stamped(
        await provider.route(route_request),
        route_request,
        provider_override=request.provider_override,
        duration_is_trustworthy=provider.capabilities.reports_trustworthy_duration,
    )
    return RouteLegResponse(
        leg=leg,
        live_update_interval_ms=provider.capabilities.live_update_interval_ms,
        # Derived here rather than on the model: the domain reports what the provider said,
        # and the boundary reports what we believe. The two differ by a factor of five on
        # dirt, because the only profile with dirt access is a bicycle one.
        estimated_duration_s=estimate_leg_duration_s(leg),
    )
