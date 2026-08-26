"""Request-scoped dependencies.

Everything is resolved from `app.state`, which `create_app` populates once at startup.
Keeping it in state (rather than module globals) is what lets tests build an app with a
fake store and offline routing without patching anything.
"""

from typing import Annotated

from fastapi import Depends, Request

from motorooter.llm.protocol import LlmClient
from motorooter.planning.discovery.details import PlaceDetails
from motorooter.planning.discovery.lookup import PlaceLookup
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.routing.policy import PolicyResolver
from motorooter.routing.registry import ProviderRegistry
from motorooter.trips.store import TripStore

OPTIONAL_SERVICES: tuple[str, ...] = ("discovery", "places", "chat_model", "place_lookup")
"""The `app.state` attributes that may legitimately be `None`, named in one place.

Each of these disables one feature when its credentials are absent, rather than refusing to
boot — discovery needs four keys, and a backend that would not start without them takes
routing and storage down for a feature most requests never touch.

Listed here so the app factory can assign them in a loop and a test can enumerate them.
`PlaceDetails` was implemented, tested, and constructed nowhere outside tests: three
hand-written assignments in `create_app`, and omitting one silently disabled an endpoint
with nothing failing anywhere. A name in a tuple is a poor abstraction and a good tripwire.
"""


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


def get_chat_model(request: Request) -> "LlmClient | None":
    """The pinned chat model, or `None` when no OpenAI key is configured.

    `None` rather than raising, for the reason discovery uses: chat needs a credential most
    requests never touch, and a backend that refused to boot without it would take routing
    and storage down with it. The endpoint answers 501 and the rest of the app works.
    """
    model: LlmClient | None = getattr(request.app.state, "chat_model", None)
    return model


def get_place_lookup(request: Request) -> "PlaceLookup | None":
    """Name-to-place search, or `None` without a Places key.

    Shared by `GET /api/geocode` and the assistant's waypoint tool, because a rider typing a
    place name and the model asking for one are the same question.
    """
    lookup: PlaceLookup | None = getattr(request.app.state, "place_lookup", None)
    return lookup


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
ChatModel = Annotated["LlmClient | None", Depends(get_chat_model)]
Lookup = Annotated["PlaceLookup | None", Depends(get_place_lookup)]
