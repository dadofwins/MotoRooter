"""Does the deployment actually get a separate photo key, and say so when it does not?

A photo URL publishes whatever key is in it. The search-side key also authorises Directions,
Geocoding and Places Text Search with no ceiling, so on anything deployed the two must differ
— and today they are the same value, which is why this exists.

The fallback keeps a prototype working. The warning is what stops it being a silent default:
a deployment publishing its server key should be noisy, and nobody reads a docstring at
deploy time.
"""

import logging

import pytest

from motorooter.api.services import build_optional_services
from motorooter.planning.discovery.factory import settings_from_env
from motorooter.routing.factory import RoutingSettings

KEYS = {
    "BRAVE_SEARCH_API_KEY": "brave-test",
    "OPENAI_API_KEY": "sk-test",
    "GOOGLE_MAPS_SERVER_KEY": "server-key",
}


@pytest.fixture
def configured(monkeypatch):
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GOOGLE_MAPS_BROWSER_KEY", raising=False)
    monkeypatch.delenv("MOTOROOTER_OFFLINE", raising=False)
    return monkeypatch


class TestTheSettingIsRead:
    def test_the_browser_key_is_picked_up(self, configured):
        configured.setenv("GOOGLE_MAPS_BROWSER_KEY", "browser-key")
        assert settings_from_env().places_photo_key == "browser-key"

    def test_it_is_none_when_absent(self, configured):
        assert settings_from_env().places_photo_key is None

    def test_an_empty_value_is_none_not_empty(self, configured):
        """An unset variable and an empty one mean the same thing, and an empty key in a URL
        would fail in a way that looks like a Places outage."""
        configured.setenv("GOOGLE_MAPS_BROWSER_KEY", "")
        assert settings_from_env().places_photo_key is None


class TestItReachesThePlacesClient:
    def test_the_photo_key_is_wired_through(self, configured):
        configured.setenv("GOOGLE_MAPS_BROWSER_KEY", "browser-key")
        places = build_optional_services(RoutingSettings())["places"]
        assert places.photo_key == "browser-key"

    def test_the_fallback_still_produces_a_usable_key(self, configured):
        places = build_optional_services(RoutingSettings())["places"]
        assert places.photo_key == "server-key"
        assert places.photo_key_is_shared is True


class TestTheFallbackIsAnnounced:
    def test_it_warns_when_one_key_serves_both(self, configured, caplog):
        caplog.set_level(logging.WARNING, logger="motorooter.api.services")
        build_optional_services(RoutingSettings())
        assert "GOOGLE_MAPS_BROWSER_KEY" in caplog.text

    def test_the_warning_says_what_is_at_stake(self, configured, caplog):
        """A warning nobody can act on is a warning nobody reads."""
        caplog.set_level(logging.WARNING, logger="motorooter.api.services")
        build_optional_services(RoutingSettings())
        assert "referrer" in caplog.text.lower()

    def test_a_separate_key_warns_about_nothing(self, configured, caplog):
        caplog.set_level(logging.WARNING, logger="motorooter.api.services")
        configured.setenv("GOOGLE_MAPS_BROWSER_KEY", "browser-key")
        build_optional_services(RoutingSettings())
        assert not caplog.records
