"""Decorator providers.

Caching, retry, and quota are written once and wrap any provider, so they are tested once
against FakeProvider rather than per adapter. Each is itself a RoutingProvider, which is
what makes them composable.
"""

import pytest

from motorooter.clock import FakeClock
from motorooter.routing.decorators.caching import CachingProvider
from motorooter.routing.decorators.quota import QuotaGuardProvider
from motorooter.routing.decorators.retry import RetryingProvider
from motorooter.routing.errors import (
    NoRouteFound,
    ProviderUnavailable,
    QuotaExceeded,
    RoutingConfigError,
)
from motorooter.routing.models import Coordinate, LegIntent, RouteRequest
from motorooter.routing.protocol import RoutingProvider
from motorooter.routing.providers.fake import FakeProvider
from tests.routing.contract import RoutingProviderContract


def req(lat: float = 45.0, lon: float = -121.0, intent: LegIntent = LegIntent.UNPAVED):
    return RouteRequest(
        waypoints=(Coordinate(lat=lat, lon=lon), Coordinate(lat=lat + 1, lon=lon)),
        intent=intent,
    )


class TestCachingSatisfiesContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self):
        return CachingProvider(FakeProvider(), clock=FakeClock())


class TestRetrySatisfiesContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self):
        return RetryingProvider(FakeProvider(), clock=FakeClock())


class TestQuotaSatisfiesContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self):
        return QuotaGuardProvider(FakeProvider(), daily_limit=1000, clock=FakeClock())


class TestCaching:
    async def test_second_identical_request_does_not_reach_upstream(self):
        inner = FakeProvider()
        cached = CachingProvider(inner, clock=FakeClock())
        await cached.route(req())
        await cached.route(req())
        assert inner.call_count == 1

    async def test_cache_hit_returns_an_equal_leg(self):
        cached = CachingProvider(FakeProvider(), clock=FakeClock())
        assert await cached.route(req()) == await cached.route(req())

    async def test_different_coordinates_miss(self):
        inner = FakeProvider()
        cached = CachingProvider(inner, clock=FakeClock())
        await cached.route(req(lat=45.0))
        await cached.route(req(lat=46.0))
        assert inner.call_count == 2

    async def test_different_intent_misses(self):
        """Same waypoints routed for dirt and for highway are different routes."""
        inner = FakeProvider()
        cached = CachingProvider(inner, clock=FakeClock())
        await cached.route(req(intent=LegIntent.UNPAVED))
        await cached.route(req(intent=LegIntent.HIGHWAY_CONNECTOR))
        assert inner.call_count == 2

    async def test_coordinates_within_rounding_precision_hit(self):
        """Sub-metre jitter during a drag must not defeat the cache."""
        inner = FakeProvider()
        cached = CachingProvider(inner, clock=FakeClock(), precision=5)
        await cached.route(req(lat=45.0))
        await cached.route(req(lat=45.0000001))
        assert inner.call_count == 1

    async def test_coordinates_beyond_precision_miss(self):
        inner = FakeProvider()
        cached = CachingProvider(inner, clock=FakeClock(), precision=5)
        await cached.route(req(lat=45.0))
        await cached.route(req(lat=45.001))
        assert inner.call_count == 2

    async def test_errors_are_not_cached(self):
        """Caching a transient failure would pin it in place until the TTL expires."""
        inner = FakeProvider(error=ProviderUnavailable("boom"), fail_first=1)
        cached = CachingProvider(inner, clock=FakeClock())
        with pytest.raises(ProviderUnavailable):
            await cached.route(req())
        leg = await cached.route(req())
        assert leg.distance_m > 0

    async def test_entries_expire_after_ttl(self):
        inner = FakeProvider()
        clock = FakeClock()
        cached = CachingProvider(inner, clock=clock, ttl_s=60.0)
        await cached.route(req())
        clock.advance(61.0)
        await cached.route(req())
        assert inner.call_count == 2

    async def test_entries_survive_until_ttl(self):
        inner = FakeProvider()
        clock = FakeClock()
        cached = CachingProvider(inner, clock=clock, ttl_s=60.0)
        await cached.route(req())
        clock.advance(59.0)
        await cached.route(req())
        assert inner.call_count == 1

    async def test_evicts_least_recently_used_when_full(self):
        inner = FakeProvider()
        cached = CachingProvider(inner, clock=FakeClock(), max_entries=2)
        await cached.route(req(lat=45.0))
        await cached.route(req(lat=46.0))
        await cached.route(req(lat=47.0))  # evicts lat=45
        await cached.route(req(lat=45.0))
        assert inner.call_count == 4

    async def test_reports_hit_and_miss_counts(self):
        cached = CachingProvider(FakeProvider(), clock=FakeClock())
        await cached.route(req())
        await cached.route(req())
        assert (cached.hits, cached.misses) == (1, 1)


class TestRetry:
    async def test_retries_retryable_errors_until_success(self):
        inner = FakeProvider(error=ProviderUnavailable("502"), fail_first=2)
        retrying = RetryingProvider(inner, clock=FakeClock(), attempts=3)
        leg = await retrying.route(req())
        assert leg.distance_m > 0
        assert inner.call_count == 3

    async def test_gives_up_after_configured_attempts(self):
        inner = FakeProvider(error=ProviderUnavailable("502"))
        retrying = RetryingProvider(inner, clock=FakeClock(), attempts=3)
        with pytest.raises(ProviderUnavailable):
            await retrying.route(req())
        assert inner.call_count == 3

    async def test_does_not_retry_non_retryable_errors(self):
        """Retrying a definitive no-route answer just wastes quota."""
        inner = FakeProvider(error=NoRouteFound("nothing there"))
        retrying = RetryingProvider(inner, clock=FakeClock(), attempts=3)
        with pytest.raises(NoRouteFound):
            await retrying.route(req())
        assert inner.call_count == 1

    async def test_does_not_retry_quota_exceeded(self):
        inner = FakeProvider(error=QuotaExceeded("daily cap"))
        retrying = RetryingProvider(inner, clock=FakeClock(), attempts=3)
        with pytest.raises(QuotaExceeded):
            await retrying.route(req())
        assert inner.call_count == 1

    async def test_backoff_is_exponential(self):
        clock = FakeClock()
        inner = FakeProvider(error=ProviderUnavailable("502"))
        retrying = RetryingProvider(inner, clock=clock, attempts=4, backoff_s=0.5)
        with pytest.raises(ProviderUnavailable):
            await retrying.route(req())
        assert clock.slept == [0.5, 1.0, 2.0]

    async def test_backoff_is_capped(self):
        clock = FakeClock()
        inner = FakeProvider(error=ProviderUnavailable("502"))
        retrying = RetryingProvider(
            inner, clock=clock, attempts=5, backoff_s=1.0, max_backoff_s=2.0
        )
        with pytest.raises(ProviderUnavailable):
            await retrying.route(req())
        assert clock.slept == [1.0, 2.0, 2.0, 2.0]

    async def test_does_not_sleep_after_the_final_attempt(self):
        clock = FakeClock()
        inner = FakeProvider(error=ProviderUnavailable("502"))
        retrying = RetryingProvider(inner, clock=clock, attempts=1)
        with pytest.raises(ProviderUnavailable):
            await retrying.route(req())
        assert clock.slept == []

    def test_rejects_zero_attempts(self):
        with pytest.raises(RoutingConfigError):
            RetryingProvider(FakeProvider(), clock=FakeClock(), attempts=0)


class TestQuotaGuard:
    async def test_allows_requests_under_the_limit(self):
        guarded = QuotaGuardProvider(FakeProvider(), daily_limit=2, clock=FakeClock())
        await guarded.route(req(lat=45.0))
        await guarded.route(req(lat=46.0))
        assert guarded.used == 2

    async def test_blocks_once_the_limit_is_reached(self):
        inner = FakeProvider()
        guarded = QuotaGuardProvider(inner, daily_limit=1, clock=FakeClock())
        await guarded.route(req(lat=45.0))
        with pytest.raises(QuotaExceeded):
            await guarded.route(req(lat=46.0))
        assert inner.call_count == 1, "blocked request must not reach upstream"

    async def test_failed_upstream_calls_still_consume_quota(self):
        """The provider charged us for that request whether or not it succeeded."""
        inner = FakeProvider(error=ProviderUnavailable("502"))
        guarded = QuotaGuardProvider(inner, daily_limit=5, clock=FakeClock())
        with pytest.raises(ProviderUnavailable):
            await guarded.route(req())
        assert guarded.used == 1

    async def test_window_resets_after_a_day(self):
        clock = FakeClock()
        guarded = QuotaGuardProvider(FakeProvider(), daily_limit=1, clock=clock)
        await guarded.route(req(lat=45.0))
        clock.advance(86_401)
        await guarded.route(req(lat=46.0))
        assert guarded.used == 1

    async def test_window_does_not_reset_early(self):
        clock = FakeClock()
        guarded = QuotaGuardProvider(FakeProvider(), daily_limit=1, clock=clock)
        await guarded.route(req(lat=45.0))
        clock.advance(86_399)
        with pytest.raises(QuotaExceeded):
            await guarded.route(req(lat=46.0))

    async def test_reports_remaining_budget(self):
        guarded = QuotaGuardProvider(FakeProvider(), daily_limit=3, clock=FakeClock())
        await guarded.route(req())
        assert guarded.remaining == 2

    async def test_defaults_to_the_providers_declared_quota(self):
        """Avoids restating a limit the capabilities already declare."""
        from motorooter.routing.models import ProviderCapabilities

        inner = FakeProvider(capabilities=ProviderCapabilities(name="ors", daily_quota=1))
        guarded = QuotaGuardProvider(inner, clock=FakeClock())
        await guarded.route(req(lat=45.0))
        with pytest.raises(QuotaExceeded):
            await guarded.route(req(lat=46.0))

    def test_requires_a_limit_when_provider_declares_none(self):
        with pytest.raises(RoutingConfigError):
            QuotaGuardProvider(FakeProvider(), clock=FakeClock())


class TestComposition:
    """Decorators must stack in any order and still be RoutingProviders."""

    def test_stack_satisfies_the_protocol(self):
        stack = CachingProvider(
            QuotaGuardProvider(
                RetryingProvider(FakeProvider(), clock=FakeClock()),
                daily_limit=10,
                clock=FakeClock(),
            ),
            clock=FakeClock(),
        )
        assert isinstance(stack, RoutingProvider)

    async def test_cache_hits_do_not_consume_quota(self):
        """The reason caching wraps quota rather than the other way round."""
        guard = QuotaGuardProvider(FakeProvider(), daily_limit=1, clock=FakeClock())
        stack = CachingProvider(guard, clock=FakeClock())
        await stack.route(req())
        await stack.route(req())
        assert guard.used == 1

    async def test_retries_share_one_cache_entry(self):
        inner = FakeProvider(error=ProviderUnavailable("502"), fail_first=1)
        stack = CachingProvider(
            RetryingProvider(inner, clock=FakeClock(), attempts=3), clock=FakeClock()
        )
        await stack.route(req())
        await stack.route(req())
        assert inner.call_count == 2  # 1 failure + 1 success, then served from cache

    def test_capabilities_pass_through_the_stack(self):
        from motorooter.routing.models import ProviderCapabilities

        caps = ProviderCapabilities(name="ors", prefers_unpaved=True, daily_quota=2000)
        stack = CachingProvider(
            QuotaGuardProvider(FakeProvider(capabilities=caps), clock=FakeClock()),
            clock=FakeClock(),
        )
        assert stack.capabilities == caps
