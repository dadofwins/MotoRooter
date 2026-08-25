"""Request-scoped dependencies.

Everything is resolved from `app.state`, which `create_app` populates once at startup.
Keeping it in state (rather than module globals) is what lets tests build an app with a
fake store and offline routing without patching anything.
"""

from typing import Annotated

from fastapi import Depends, Request

from motorooter.routing.policy import PolicyResolver
from motorooter.routing.registry import ProviderRegistry
from motorooter.trips.store import TripStore


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.provider_registry
    return registry


def get_resolver(request: Request) -> PolicyResolver:
    resolver: PolicyResolver = request.app.state.policy_resolver
    return resolver


def get_trip_store(request: Request) -> TripStore:
    store: TripStore = request.app.state.trip_store
    return store


Registry = Annotated[ProviderRegistry, Depends(get_registry)]
Resolver = Annotated[PolicyResolver, Depends(get_resolver)]
Trips = Annotated[TripStore, Depends(get_trip_store)]
