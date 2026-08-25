"""Assembly of the routing stack from settings.

The factory is the only place provider names, decorator order, and the default policy
table are chosen. Everything downstream sees a `PolicyResolver`.
"""

import pytest

from motorooter.routing.errors import RoutingConfigError
from motorooter.routing.factory import RoutingSettings, build_routing
from motorooter.routing.models import LegIntent


class TestFakeMode:
    def test_offline_mode_needs_no_api_keys(self):
        """Local development and CI must work with no credentials at all."""
        registry, resolver = build_routing(RoutingSettings(offline=True))
        assert registry.names() == ["fake"]
        assert resolver.resolve(LegIntent.UNPAVED).capabilities.name == "fake"

    def test_offline_mode_covers_every_intent(self):
        _, resolver = build_routing(RoutingSettings(offline=True))
        for intent in LegIntent:
            assert resolver.resolve(intent) is not None


class TestLiveMode:
    @pytest.fixture
    def settings(self):
        return RoutingSettings(ors_api_key="ors-key", google_api_key="google-key")

    def test_registers_both_providers(self, settings):
        registry, _ = build_routing(settings)
        assert set(registry.names()) == {"ors", "google"}

    def test_dirt_intents_route_through_ors(self, settings):
        _, resolver = build_routing(settings)
        for intent in (LegIntent.UNPAVED, LegIntent.TECHNICAL_OFFROAD):
            assert resolver.resolve(intent).capabilities.name == "ors"

    def test_paved_intents_route_through_google(self, settings):
        _, resolver = build_routing(settings)
        for intent in (LegIntent.HIGHWAY_CONNECTOR, LegIntent.TWISTY_PAVED):
            assert resolver.resolve(intent).capabilities.name == "google"

    def test_missing_ors_key_is_a_config_error(self):
        with pytest.raises(RoutingConfigError, match="ors_api_key"):
            build_routing(RoutingSettings(google_api_key="google-key"))

    def test_missing_google_key_is_a_config_error(self):
        with pytest.raises(RoutingConfigError, match="google_api_key"):
            build_routing(RoutingSettings(ors_api_key="ors-key"))


class TestDecoratorStack:
    @pytest.fixture
    def registry(self):
        return build_routing(RoutingSettings(offline=True))[0]

    def test_capabilities_survive_the_wrapping(self, registry):
        """Throttle intervals and quotas must not be masked by decorators."""
        caps = registry.get("fake").capabilities
        assert caps.name == "fake"
        assert caps.live_update_interval_ms == 0

    async def test_caching_is_active(self, registry):
        from motorooter.routing.models import Coordinate, RouteRequest

        provider = registry.get("fake")
        request = RouteRequest(
            waypoints=(Coordinate(lat=45.0, lon=-121.0), Coordinate(lat=46.0, lon=-121.0)),
            intent=LegIntent.UNPAVED,
        )
        first = await provider.route(request)
        second = await provider.route(request)
        assert first == second

    def test_providers_without_declared_quota_are_not_quota_guarded(self):
        """Google declares no daily cap; wrapping it in a guard would invent one."""
        registry, _ = build_routing(RoutingSettings(ors_api_key="k", google_api_key="k"))
        assert registry.get("google").capabilities.daily_quota is None


class TestPolicyOverrides:
    def test_intent_provider_overrides_are_applied(self):
        """Retuning which engine handles a road type is a config change, not a code change."""
        _, resolver = build_routing(
            RoutingSettings(
                ors_api_key="k",
                google_api_key="k",
                intent_provider_overrides={LegIntent.TWISTY_PAVED: "ors"},
            )
        )
        assert resolver.resolve(LegIntent.TWISTY_PAVED).capabilities.name == "ors"

    def test_override_to_unregistered_provider_fails_at_startup(self):
        with pytest.raises(RoutingConfigError, match="valhalla"):
            build_routing(
                RoutingSettings(
                    ors_api_key="k",
                    google_api_key="k",
                    intent_provider_overrides={LegIntent.UNPAVED: "valhalla"},
                )
            )

    def test_override_violating_a_capability_fails_at_startup(self):
        """Pointing dirt legs at a paved-only engine must break the deploy."""
        with pytest.raises(RoutingConfigError, match="prefers_unpaved"):
            build_routing(
                RoutingSettings(
                    ors_api_key="k",
                    google_api_key="k",
                    intent_provider_overrides={LegIntent.TECHNICAL_OFFROAD: "google"},
                )
            )


class TestSelfHostedOrs:
    def test_custom_base_url_drops_the_hosted_quota(self):
        """A self-hosted instance has no free-tier cap, and should not be throttled like one."""
        registry, _ = build_routing(
            RoutingSettings(
                ors_api_key="k",
                google_api_key="k",
                ors_base_url="http://ors.internal:8080",
            )
        )
        caps = registry.get("ors").capabilities
        assert caps.daily_quota is None
        assert caps.live_update_interval_ms == 1000
