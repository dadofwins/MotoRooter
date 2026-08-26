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

import logging
from typing import Any

from motorooter.api.deps import OPTIONAL_SERVICES
from motorooter.chat.factory import build_chat_model
from motorooter.planning.discovery.details import PlaceDetails
from motorooter.planning.discovery.factory import DiscoverySettings, build_discovery
from motorooter.planning.discovery.factory import settings_from_env as discovery_from_env
from motorooter.routing.factory import RoutingSettings

logger = logging.getLogger(__name__)


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
        "places": _places(settings),
    }


def _places(settings: DiscoverySettings) -> PlaceDetails | None:
    """The Places detail client, announcing a shared photo key if that is what it got.

    Photo URLs publish whatever key they carry. Falling back to the server key keeps a
    prototype working, and warning about it is what stops that becoming a silent default —
    nobody reads a docstring at deploy time, and a public URL turns this from a note into
    billing exposure with no ceiling.
    """
    if not settings.places_api_key:
        return None
    places = PlaceDetails(api_key=settings.places_api_key, photo_key=settings.places_photo_key)
    if places.photo_key_is_shared:
        logger.warning(
            "GOOGLE_MAPS_BROWSER_KEY is not set, so POI photo URLs will carry the "
            "server-side Places key into the browser. That key also authorises Directions, "
            "Geocoding and Places Text Search with no spend ceiling. Before deploying, "
            "provision a browser key restricted by HTTP referrer and to Places Photos."
        )
    return places
