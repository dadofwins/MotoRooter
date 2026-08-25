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
from motorooter.api.routers import places, routing, trips
from motorooter.api.schemas import HealthResponse
from motorooter.api.streaming import apply_streaming_media_types
from motorooter.routing.factory import RoutingSettings, build_routing
from motorooter.trips.store import InMemoryTripStore, TripStore

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
    trip_store: TripStore | None = None,
) -> FastAPI:
    """Build the application.

    Routing is wired here so a misconfigured policy raises `RoutingConfigError` at startup
    and fails the deploy, rather than surfacing on a user's first dirt leg.

    `trip_store` defaults to the in-memory implementation, which is correct for local
    development and tests but loses everything on restart. Production must inject a
    durable store — Cloud Run's filesystem is ephemeral and per-instance.
    """
    app = FastAPI(title="MotoRooter", version="0.1.0")

    registry, resolver = build_routing(settings or routing_settings_from_env())
    app.state.provider_registry = registry
    app.state.policy_resolver = resolver
    app.state.trip_store = trip_store or InMemoryTripStore()

    register_exception_handlers(app)

    @app.get("/api/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", providers=registry.names())

    app.include_router(routing.router)
    app.include_router(trips.router)
    app.include_router(places.router)

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
