"""FastAPI application.

Serves both the API and the built React bundle, so production runs as a single Cloud Run
service with one origin and no CORS. The frontend has no runtime of its own — Vite compiles
it to static files at build time.
"""

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from motorooter.api.exception_handlers import register_exception_handlers
from motorooter.api.routers import geocode, places, routing, trips
from motorooter.api.schemas import HealthResponse
from motorooter.api.services import build_optional_services
from motorooter.api.streaming import apply_streaming_media_types
from motorooter.routing.factory import RoutingSettings, build_routing
from motorooter.trips.factory import TripStorageSettings, build_trip_store
from motorooter.trips.factory import settings_from_env as storage_settings_from_env
from motorooter.trips.store import TripStore

STATIC_DIR = Path(os.environ.get("MOTOROOTER_STATIC_DIR", "static"))


def routing_settings_from_env() -> RoutingSettings:
    """Read routing config from the environment.

    Keys come from Secret Manager in Cloud Run and a gitignored `.env` locally. Setting
    `MOTOROOTER_OFFLINE=1` runs against `FakeProvider` with no credentials at all.
    """
    return RoutingSettings(
        ors_api_key=os.environ.get("ORS_API_KEY"),
        google_api_key=os.environ.get("GOOGLE_MAPS_SERVER_KEY"),
        ors_base_url=os.environ.get("ORS_BASE_URL", RoutingSettings.ors_base_url),
        offline=os.environ.get("MOTOROOTER_OFFLINE") == "1",
    )


def create_app(
    settings: RoutingSettings | None = None,
    *,
    storage_settings: TripStorageSettings | None = None,
    trip_store: TripStore | None = None,
) -> FastAPI:
    """Build the application.

    Routing is wired here so a misconfigured policy raises `RoutingConfigError` at startup
    and fails the deploy, rather than surfacing on a user's first dirt leg.

    Storage defaults to whatever the environment configures: Cloud Storage when
    `MOTOROOTER_TRIPS_BUCKET` is set, otherwise the in-memory implementation, which is
    correct for local development and tests but loses everything on restart. Production
    must set the bucket — Cloud Run's filesystem is ephemeral and per-instance.
    """
    app = FastAPI(title="MotoRooter", version="0.1.0")

    routing_config = settings or routing_settings_from_env()
    registry, resolver = build_routing(routing_config)
    app.state.provider_registry = registry
    app.state.policy_resolver = resolver
    app.state.trip_store = trip_store or build_trip_store(
        storage_settings or _storage_settings_for(routing_config)
    )

    # Assigned in a loop from one place, because three hand-written assignments is how
    # `PlaceDetails` came to be fully implemented, fully tested, and constructed nowhere:
    # the endpoint read `app.state.places`, found nothing, and answered 501 forever while
    # every test wired the attribute itself. A missing service now fails a test that
    # enumerates `OPTIONAL_SERVICES` rather than failing silently in production.
    for name, service in build_optional_services(routing_config).items():
        setattr(app.state, name, service)

    register_exception_handlers(app)

    @app.get("/api/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", providers=registry.names())

    app.include_router(routing.router)
    app.include_router(trips.router)
    app.include_router(places.router)
    app.include_router(geocode.router)

    _mount_frontend(app)
    _install_openapi_postprocess(app)
    return app


def _install_openapi_postprocess(app: FastAPI) -> None:
    """Correct streaming media types in the generated document.

    The frontend's TypeScript is generated from this schema, so it must describe what the
    server actually sends.
    """
    base = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = apply_streaming_media_types(base())
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def _storage_settings_for(routing_config: RoutingSettings) -> TripStorageSettings:
    """Offline routing implies ephemeral storage; the reverse does not hold.

    Reading the bucket out of the ambient environment in offline mode would make the test
    suite depend on the developer's shell, and a stray `MOTOROOTER_TRIPS_BUCKET` would
    quietly point it at someone's real bucket. Ephemeral storage on its own is set through
    the environment like any other storage setting.
    """
    if routing_config.offline:
        return TripStorageSettings(ephemeral=True)
    return storage_settings_from_env()


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built React bundle, if present.

    Absent during backend-only development and in tests; the API works either way.
    """
    if not STATIC_DIR.is_dir():
        return

    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    # Registered last so it cannot shadow an API route. Without it, deep links and page
    # refreshes 404 instead of loading the SPA.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
