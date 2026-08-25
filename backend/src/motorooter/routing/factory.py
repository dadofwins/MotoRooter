"""Assembles the routing stack from settings.

This is the single place that names concrete providers, picks decorator order, and defines
the default intent -> provider table. Everything downstream depends only on
`PolicyResolver`, so swapping an engine is a change here and nowhere else.
"""

import dataclasses
from collections.abc import Mapping

from motorooter.clock import Clock, SystemClock
from motorooter.routing.decorators.caching import CachingProvider
from motorooter.routing.decorators.quota import SECONDS_PER_MINUTE, QuotaGuardProvider
from motorooter.routing.decorators.retry import RetryingProvider
from motorooter.routing.errors import RateLimited, RoutingConfigError
from motorooter.routing.models import LegIntent
from motorooter.routing.policy import IntentPolicy, PolicyResolver
from motorooter.routing.protocol import RoutingProvider
from motorooter.routing.providers import google as google_provider
from motorooter.routing.providers import ors as ors_provider
from motorooter.routing.providers.fake import FakeProvider
from motorooter.routing.registry import ProviderRegistry

DEFAULT_POLICY_TABLE: Mapping[LegIntent, IntentPolicy] = {
    LegIntent.HIGHWAY_CONNECTOR: IntentPolicy(provider="google"),
    LegIntent.TWISTY_PAVED: IntentPolicy(provider="google"),
    LegIntent.UNPAVED: IntentPolicy(provider="ors", requires_unpaved=True),
    LegIntent.TECHNICAL_OFFROAD: IntentPolicy(provider="ors", requires_unpaved=True),
    LegIntent.MANUAL_TRACK: IntentPolicy(provider="google"),
}

SELF_HOSTED_LIVE_UPDATE_INTERVAL_MS = 1000
"""Own instance, own hardware: refresh during a drag as freely as a cheap provider."""


@dataclasses.dataclass(frozen=True)
class RoutingSettings:
    """Everything the routing layer needs to wire itself up."""

    ors_api_key: str | None = None
    google_api_key: str | None = None

    ors_base_url: str = ors_provider.ORS_BASE_URL
    """Point at a self-hosted ORS to escape the free tier; quota and throttle relax to match."""

    offline: bool = False
    """Register only `FakeProvider`. For local development and CI, needs no credentials."""

    intent_provider_overrides: Mapping[LegIntent, str] = dataclasses.field(default_factory=dict)
    """Per-intent provider reassignment, validated at startup like the rest of the table."""

    cache_ttl_s: float | None = 3600.0
    cache_max_entries: int = 1024
    retry_attempts: int = 3
    retry_backoff_s: float = 0.25


def build_routing(
    settings: RoutingSettings,
    *,
    clock: Clock | None = None,
) -> tuple[ProviderRegistry, PolicyResolver]:
    """Build the provider registry and policy resolver.

    Raises:
        RoutingConfigError: missing credentials, or a policy the providers cannot satisfy.
            Deliberately raised at startup so misconfiguration fails the deploy.
    """
    clock = clock or SystemClock()
    providers = [FakeProvider()] if settings.offline else _live_providers(settings)
    registry = ProviderRegistry([_decorate(p, settings, clock) for p in providers])
    return registry, PolicyResolver(registry, _policy_table(settings))


def _live_providers(settings: RoutingSettings) -> list[RoutingProvider]:
    if not settings.ors_api_key:
        msg = "ors_api_key is required unless offline=True"
        raise RoutingConfigError(msg)
    if not settings.google_api_key:
        msg = "google_api_key is required unless offline=True"
        raise RoutingConfigError(msg)

    capabilities = ors_provider.CAPABILITIES
    if settings.ors_base_url != ors_provider.ORS_BASE_URL:
        # Self-hosted: the free-tier cap and its conservative drag throttle do not apply.
        capabilities = capabilities.model_copy(
            update={
                "daily_quota": None,
                "per_minute_quota": None,
                "live_update_interval_ms": SELF_HOSTED_LIVE_UPDATE_INTERVAL_MS,
            }
        )

    return [
        ors_provider.OrsProvider(
            api_key=settings.ors_api_key,
            base_url=settings.ors_base_url,
            capabilities=capabilities,
        ),
        google_provider.GoogleDirectionsProvider(api_key=settings.google_api_key),
    ]


def _decorate(
    provider: RoutingProvider, settings: RoutingSettings, clock: Clock
) -> RoutingProvider:
    """Wrap a provider in the standard stack.

    Order matters. Retry is innermost so each attempt is charged as the separate upstream
    request it is; quota sits above it; caching is outermost so a hit costs no budget.
    Providers that declare no quota are not guarded — inventing a cap would be wrong.
    """
    wrapped: RoutingProvider = RetryingProvider(
        provider,
        clock=clock,
        attempts=settings.retry_attempts,
        backoff_s=settings.retry_backoff_s,
    )
    # One guard per declared window, innermost first, so a call is charged to both. The
    # per-minute ceiling is a different limit from the daily one, not a fraction of it:
    # enforcing only the daily cap lets a burst — a discovery fan-out, or a fast drag —
    # sail past the local guard and come back as an opaque upstream failure.
    if provider.capabilities.per_minute_quota is not None:
        wrapped = QuotaGuardProvider(
            wrapped,
            clock=clock,
            window_s=SECONDS_PER_MINUTE,
            window_name="per-minute",
            exhausted=RateLimited,
        )
    if provider.capabilities.daily_quota is not None:
        wrapped = QuotaGuardProvider(wrapped, clock=clock)
    return CachingProvider(
        wrapped,
        clock=clock,
        ttl_s=settings.cache_ttl_s,
        max_entries=settings.cache_max_entries,
    )


def _policy_table(settings: RoutingSettings) -> Mapping[LegIntent, IntentPolicy]:
    if settings.offline:
        return {intent: IntentPolicy(provider="fake") for intent in LegIntent}

    table = dict(DEFAULT_POLICY_TABLE)
    for intent, provider_name in settings.intent_provider_overrides.items():
        # Keep the intent's capability requirements; only the provider changes. An override
        # that cannot meet them fails in PolicyResolver's startup validation.
        table[intent] = table[intent].model_copy(update={"provider": provider_name})
    return table
