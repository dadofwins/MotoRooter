"""The services that may be absent, built in one place.

Each of these disables one feature when its credentials are missing rather than refusing to
boot: discovery needs four API keys, and a backend that would not start without them takes
routing and storage down for a feature most requests never touch. The endpoints answer 501
and everything else works.

**Built together because they were forgotten separately.** `PlaceDetails` was implemented,
tested, and constructed nowhere outside tests — the endpoint read `app.state.places`, found
nothing, and answered 501 for every request while every test assigned the attribute itself.
Three hand-written assignments in `create_app` is a shape where omitting one fails nothing.
Returning a mapping keyed by `OPTIONAL_SERVICES` means a new service is added in one place
and a missing one fails an enumerating test.
"""

from typing import Any

from motorooter.api.deps import OPTIONAL_SERVICES
from motorooter.chat.factory import build_chat_model
from motorooter.planning.discovery.details import PlaceDetails
from motorooter.planning.discovery.factory import build_discovery
from motorooter.planning.discovery.factory import settings_from_env as discovery_from_env
from motorooter.routing.factory import RoutingSettings


def build_optional_services(routing_config: RoutingSettings) -> dict[str, Any]:
    """Every optional service, or `None` for each that cannot be built.

    Offline disables all of them rather than some: `MOTOROOTER_OFFLINE=1` means no external
    services at all, and a half-offline app is a worse thing to debug than either extreme.
    """
    services = _build(routing_config)

    # A startup failure rather than a silent 501. Checked on every path including offline,
    # because the offline branch returning a dict keyed off `OPTIONAL_SERVICES` was itself a
    # way to satisfy the check without building anything — a guard that the shortcut walks
    # straight past is not a guard.
    missing = sorted(set(OPTIONAL_SERVICES) - set(services))
    if missing:
        msg = f"optional services declared but not built: {', '.join(missing)}"
        raise RuntimeError(msg)
    return services


def _build(routing_config: RoutingSettings) -> dict[str, Any]:
    """Each service, or `None`. Every name in `OPTIONAL_SERVICES` must appear here."""
    if routing_config.offline:
        return {"discovery": None, "chat_model": None, "places": None}

    settings = discovery_from_env()
    return {
        "discovery": build_discovery(settings),
        "chat_model": build_chat_model(settings),
        # Needs only the one key, unlike discovery, so it is built directly rather than
        # gated behind `settings.configured` — the POI dialog should work on a deployment
        # with a Maps key and no search credentials.
        "places": PlaceDetails(api_key=settings.places_api_key)
        if settings.places_api_key
        else None,
    }
