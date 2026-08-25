"""Request-scoped dependencies.

Everything is resolved from `app.state`, which `create_app` populates once at startup.
Keeping it in state (rather than module globals) is what lets tests build an app with a
fake store and offline routing without patching anything.
"""

from typing import Annotated

from fastapi import Depends, Request

from motorooter.planning.discovery.details import PlaceDetails
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.routing.policy import PolicyResolver
from motorooter.routing.registry import ProviderRegistry
from motorooter.trips.store import TripStore


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.provider_registry
    return registry


def get_resolver(request: Request) -> PolicyResolver:
    resolver: PolicyResolver = request.app.state.policy_resolver
    return resolver


def get_discovery(request: Request) -> "DiscoveryPipeline | None":
    """The discovery pipeline, or `None` when it could not be built.

    `None` rather than raising at startup: discovery needs four API keys, and a backend that
    refuses to boot without them would make every other endpoint unavailable for want of a
    feature most requests do not use. Replan reports it; the rest of the app runs.
    """
    pipeline: DiscoveryPipeline | None = getattr(request.app.state, "discovery", None)
    return pipeline


def get_places(request: Request) -> "PlaceDetails | None":
    """The Places detail client, or `None` when no key is configured."""
    places: PlaceDetails | None = getattr(request.app.state, "places", None)
    return places


def get_trip_store(request: Request) -> TripStore:
    store: TripStore = request.app.state.trip_store
    return store


Registry = Annotated[ProviderRegistry, Depends(get_registry)]
Resolver = Annotated[PolicyResolver, Depends(get_resolver)]
Trips = Annotated[TripStore, Depends(get_trip_store)]
Discovery = Annotated["DiscoveryPipeline | None", Depends(get_discovery)]
Places = Annotated["PlaceDetails | None", Depends(get_places)]
