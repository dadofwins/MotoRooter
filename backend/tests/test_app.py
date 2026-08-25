"""Application wiring."""

import pytest
from fastapi.testclient import TestClient

from motorooter.app import create_app
from motorooter.routing.errors import RoutingConfigError
from motorooter.routing.factory import RoutingSettings
from motorooter.routing.models import LegIntent


@pytest.fixture
def client():
    return TestClient(create_app(RoutingSettings(offline=True)))


def test_health_reports_registered_providers(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "providers": ["fake"]}


def test_capabilities_expose_every_configured_intent(client):
    intents = client.get("/api/routing/capabilities").json()["intents"]
    assert set(intents) == {intent.value for intent in LegIntent}


def test_capabilities_expose_the_live_update_interval(client):
    """The frontend drag throttle reads this instead of hardcoding an engine name."""
    intents = client.get("/api/routing/capabilities").json()["intents"]
    assert intents[LegIntent.UNPAVED.value]["live_update_interval_ms"] == 0


def test_capabilities_name_the_resolved_provider(client):
    intents = client.get("/api/routing/capabilities").json()["intents"]
    assert intents[LegIntent.TECHNICAL_OFFROAD.value]["provider"] == "fake"


def test_misconfiguration_fails_at_startup_not_at_request_time():
    """A bad policy must break the deploy, not the first ride."""
    with pytest.raises(RoutingConfigError):
        create_app(RoutingSettings(google_api_key="only-one-key"))
