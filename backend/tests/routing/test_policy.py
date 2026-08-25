"""Policy resolver: LegIntent -> provider.

This is where "a different routing algorithm per road type" lives. The table is config,
so swapping engines per intent never touches routing code. Misconfiguration must surface
at startup, not on a user's first dirt leg.
"""

import pytest

from motorooter.routing.errors import ProviderNotFound, RoutingConfigError, UnsupportedIntent
from motorooter.routing.models import LegIntent, ProviderCapabilities
from motorooter.routing.policy import IntentPolicy, PolicyResolver
from motorooter.routing.providers.fake import FakeProvider
from motorooter.routing.registry import ProviderRegistry


def fake(name: str, **caps) -> FakeProvider:
    return FakeProvider(capabilities=ProviderCapabilities(name=name, **caps))


@pytest.fixture
def registry():
    return ProviderRegistry(
        [
            fake("google", max_waypoints=25, live_update_interval_ms=1000),
            fake("ors", prefers_unpaved=True, elevation=True, live_update_interval_ms=3000),
            fake("manual", max_waypoints=1000, live_update_interval_ms=0),
        ]
    )


@pytest.fixture
def table():
    return {
        LegIntent.HIGHWAY_CONNECTOR: IntentPolicy(provider="google"),
        LegIntent.TWISTY_PAVED: IntentPolicy(provider="google"),
        LegIntent.UNPAVED: IntentPolicy(provider="ors", requires_unpaved=True),
        LegIntent.TECHNICAL_OFFROAD: IntentPolicy(provider="ors", requires_unpaved=True),
        LegIntent.MANUAL_TRACK: IntentPolicy(provider="manual"),
    }


class TestResolution:
    def test_maps_intent_to_configured_provider(self, registry, table):
        resolver = PolicyResolver(registry, table)
        assert resolver.resolve(LegIntent.UNPAVED).capabilities.name == "ors"

    def test_different_intents_resolve_to_different_providers(self, registry, table):
        resolver = PolicyResolver(registry, table)
        assert resolver.resolve(LegIntent.HIGHWAY_CONNECTOR).capabilities.name == "google"
        assert resolver.resolve(LegIntent.TECHNICAL_OFFROAD).capabilities.name == "ors"

    def test_per_leg_override_wins(self, registry, table):
        """Users can pin one section to a specific engine without changing config."""
        resolver = PolicyResolver(registry, table)
        chosen = resolver.resolve(LegIntent.UNPAVED, override="google")
        assert chosen.capabilities.name == "google"

    def test_unknown_override_raises_provider_not_found(self, registry, table):
        resolver = PolicyResolver(registry, table)
        with pytest.raises(ProviderNotFound):
            resolver.resolve(LegIntent.UNPAVED, override="valhalla")

    def test_intent_missing_from_table_raises(self, registry):
        resolver = PolicyResolver(registry, {LegIntent.UNPAVED: IntentPolicy(provider="ors")})
        with pytest.raises(UnsupportedIntent, match="highway_connector"):
            resolver.resolve(LegIntent.HIGHWAY_CONNECTOR)

    def test_policy_params_are_exposed_for_the_adapter(self, registry):
        table = {
            LegIntent.UNPAVED: IntentPolicy(provider="ors", profile_params={"preference": "dirt"})
        }
        resolver = PolicyResolver(registry, table)
        assert resolver.policy_for(LegIntent.UNPAVED).profile_params == {"preference": "dirt"}


class TestStartupValidation:
    """Every one of these must fail at construction, not on first request."""

    def test_rejects_table_referencing_unregistered_provider(self, registry):
        table = {LegIntent.UNPAVED: IntentPolicy(provider="valhalla")}
        with pytest.raises(RoutingConfigError, match="valhalla"):
            PolicyResolver(registry, table)

    def test_rejects_provider_lacking_a_required_capability(self, registry):
        """Routing dirt through a paved-only engine is the exact bug this prevents."""
        table = {LegIntent.UNPAVED: IntentPolicy(provider="google", requires_unpaved=True)}
        with pytest.raises(RoutingConfigError, match="prefers_unpaved"):
            PolicyResolver(registry, table)

    def test_accepts_provider_meeting_all_requirements(self, registry):
        table = {
            LegIntent.UNPAVED: IntentPolicy(
                provider="ors", requires_unpaved=True, requires_elevation=True
            )
        }
        PolicyResolver(registry, table)  # must not raise

    def test_rejects_empty_table(self, registry):
        with pytest.raises(RoutingConfigError):
            PolicyResolver(registry, {})


class TestLiveUpdateBudget:
    """Feeds the frontend's per-provider drag throttle."""

    def test_reports_interval_for_the_resolved_provider(self, registry, table):
        resolver = PolicyResolver(registry, table)
        assert resolver.live_update_interval_ms(LegIntent.HIGHWAY_CONNECTOR) == 1000
        assert resolver.live_update_interval_ms(LegIntent.UNPAVED) == 3000

    def test_none_interval_means_preview_only(self, table):
        registry = ProviderRegistry([fake("ors", prefers_unpaved=True)])
        table = {LegIntent.UNPAVED: IntentPolicy(provider="ors", requires_unpaved=True)}
        resolver = PolicyResolver(registry, table)
        assert resolver.live_update_interval_ms(LegIntent.UNPAVED) is None
