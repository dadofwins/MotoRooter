"""FastAPI application.

Serves both the API and the built React bundle, so production runs as a single Cloud Run
service with one origin and no CORS. The frontend has no runtime of its own — Vite compiles
it to static files at build time.
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from motorooter.routing.factory import RoutingSettings, build_routing

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


def create_app(settings: RoutingSettings | None = None) -> FastAPI:
    """Build the application.

    Routing is wired here so a misconfigured policy raises `RoutingConfigError` at startup
    and fails the deploy, rather than surfacing on a user's first dirt leg.
    """
    app = FastAPI(title="MotoRooter", version="0.1.0")
    registry, resolver = build_routing(settings or routing_settings_from_env())
    app.state.provider_registry = registry
    app.state.policy_resolver = resolver

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "providers": registry.names()}

    @app.get("/api/routing/capabilities")
    async def capabilities() -> dict[str, object]:
        """Lets the frontend throttle per provider without hardcoding an engine name."""
        return {
            "providers": [p.capabilities.model_dump() for p in registry],
            "intents": {
                intent.value: {
                    "provider": resolver.resolve(intent).capabilities.name,
                    "live_update_interval_ms": resolver.live_update_interval_ms(intent),
                }
                for intent in resolver.configured_intents()
            },
        }

    if STATIC_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str) -> FileResponse:
            """Serve index.html for unmatched paths.

            Without this, deep links and page refreshes 404 instead of loading the SPA.
            """
            return FileResponse(STATIC_DIR / "index.html")

    return app
