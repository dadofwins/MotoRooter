"""Provider registry: name -> provider, plus capability-based lookup."""

import pytest

from motorooter.routing.errors import ProviderNotFound, RoutingConfigError
from motorooter.routing.models import ProviderCapabilities
from motorooter.routing.providers.fake import FakeProvider
from motorooter.routing.registry import ProviderRegistry


def fake(name: str, **caps) -> FakeProvider:
    return FakeProvider(capabilities=ProviderCapabilities(name=name, **caps))


class TestRegistration:
    def test_registers_under_its_capability_name(self):
        registry = ProviderRegistry([fake("ors")])
        assert registry.get("ors").capabilities.name == "ors"

    def test_unknown_name_raises_provider_not_found(self):
        registry = ProviderRegistry([fake("ors")])
        with pytest.raises(ProviderNotFound):
            registry.get("valhalla")

    def test_error_lists_available_providers(self):
        """Misconfiguration is far easier to fix when the message shows the options."""
        registry = ProviderRegistry([fake("ors"), fake("google")])
        with pytest.raises(ProviderNotFound, match="google"):
            registry.get("valhalla")

    def test_duplicate_names_are_rejected(self):
        """Silent last-wins registration makes provider choice depend on import order."""
        with pytest.raises(RoutingConfigError, match="ors"):
            ProviderRegistry([fake("ors"), fake("ors")])

    def test_names_are_reported(self):
        registry = ProviderRegistry([fake("ors"), fake("google")])
        assert set(registry.names()) == {"ors", "google"}

    def test_contains(self):
        registry = ProviderRegistry([fake("ors")])
        assert "ors" in registry
        assert "valhalla" not in registry


class TestCapabilityLookup:
    def test_finds_providers_matching_all_requirements(self):
        registry = ProviderRegistry(
            [fake("ors", prefers_unpaved=True), fake("google"), fake("valhalla", elevation=True)]
        )
        found = registry.find(prefers_unpaved=True)
        assert [p.capabilities.name for p in found] == ["ors"]

    def test_requirements_are_conjunctive(self):
        registry = ProviderRegistry(
            [
                fake("ors", prefers_unpaved=True),
                fake("valhalla", prefers_unpaved=True, elevation=True),
            ]
        )
        found = registry.find(prefers_unpaved=True, elevation=True)
        assert [p.capabilities.name for p in found] == ["valhalla"]

    def test_no_requirements_returns_everything(self):
        registry = ProviderRegistry([fake("ors"), fake("google")])
        assert len(registry.find()) == 2

    def test_returns_empty_when_nothing_matches(self):
        registry = ProviderRegistry([fake("google")])
        assert registry.find(prefers_unpaved=True) == []

    def test_unknown_capability_name_is_a_config_error(self):
        """A typo'd capability would otherwise silently match nothing."""
        registry = ProviderRegistry([fake("ors")])
        with pytest.raises(RoutingConfigError, match="prefers_dirt"):
            registry.find(prefers_dirt=True)
