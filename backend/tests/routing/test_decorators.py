"""Decorator providers.

Caching, retry, and quota are written once and wrap any provider, so they are tested once
against FakeProvider rather than per adapter. Each is itself a RoutingProvider, which is
what makes them composable.
"""

import pytest

from motorooter.clock import FakeClock
from motorooter.routing.decorators.caching import CachingProvider
from motorooter.routing.decorators.coincident import CoincidentSpanProvider
from motorooter.routing.decorators.quota import QuotaGuardProvider
from motorooter.routing.decorators.retry import RetryingProvider
from motorooter.routing.errors import (
    NoRouteFound,
    ProviderUnavailable,
    QuotaExceeded,
    RoutingConfigError,
)
from motorooter.routing.geo import COINCIDENT_TOLERANCE_M
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

    @pytest.fixture
    def degenerate_upstream(self):
        """None: wrapping `FakeProvider`, which builds its own geometry. A decorator
        forwards whatever its inner provider raises rather than parsing a reply itself."""
        return None


class TestRetrySatisfiesContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self):
        return RetryingProvider(FakeProvider(), clock=FakeClock())

    @pytest.fixture
    def degenerate_upstream(self):
        """None: wrapping `FakeProvider`, which builds its own geometry. A decorator
        forwards whatever its inner provider raises rather than parsing a reply itself."""
        return None


class TestQuotaSatisfiesContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self):
        return QuotaGuardProvider(FakeProvider(), limit=1000, clock=FakeClock())

    @pytest.fixture
    def degenerate_upstream(self):
        """None: wrapping `FakeProvider`, which builds its own geometry. A decorator
        forwards whatever its inner provider raises rather than parsing a reply itself."""
        return None


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
        guarded = QuotaGuardProvider(FakeProvider(), limit=2, clock=FakeClock())
        await guarded.route(req(lat=45.0))
        await guarded.route(req(lat=46.0))
        assert guarded.used == 2

    async def test_blocks_once_the_limit_is_reached(self):
        inner = FakeProvider()
        guarded = QuotaGuardProvider(inner, limit=1, clock=FakeClock())
        await guarded.route(req(lat=45.0))
        with pytest.raises(QuotaExceeded):
            await guarded.route(req(lat=46.0))
        assert inner.call_count == 1, "blocked request must not reach upstream"

    async def test_failed_upstream_calls_still_consume_quota(self):
        """The provider charged us for that request whether or not it succeeded."""
        inner = FakeProvider(error=ProviderUnavailable("502"))
        guarded = QuotaGuardProvider(inner, limit=5, clock=FakeClock())
        with pytest.raises(ProviderUnavailable):
            await guarded.route(req())
        assert guarded.used == 1

    async def test_window_resets_after_a_day(self):
        clock = FakeClock()
        guarded = QuotaGuardProvider(FakeProvider(), limit=1, clock=clock)
        await guarded.route(req(lat=45.0))
        clock.advance(86_401)
        await guarded.route(req(lat=46.0))
        assert guarded.used == 1

    async def test_window_does_not_reset_early(self):
        clock = FakeClock()
        guarded = QuotaGuardProvider(FakeProvider(), limit=1, clock=clock)
        await guarded.route(req(lat=45.0))
        clock.advance(86_399)
        with pytest.raises(QuotaExceeded):
            await guarded.route(req(lat=46.0))

    async def test_reports_remaining_budget(self):
        guarded = QuotaGuardProvider(FakeProvider(), limit=3, clock=FakeClock())
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
                limit=10,
                clock=FakeClock(),
            ),
            clock=FakeClock(),
        )
        assert isinstance(stack, RoutingProvider)

    async def test_cache_hits_do_not_consume_quota(self):
        """The reason caching wraps quota rather than the other way round."""
        guard = QuotaGuardProvider(FakeProvider(), limit=1, clock=FakeClock())
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


class TestCoincidentSatisfiesContract(RoutingProviderContract):
    @pytest.fixture
    def provider(self):
        return CoincidentSpanProvider(FakeProvider())

    @pytest.fixture
    def degenerate_upstream(self):
        """None: wrapping `FakeProvider`, which builds its own geometry. A decorator
        forwards whatever its inner provider raises rather than parsing a reply itself."""
        return None


def span(*points: tuple[float, float], intent: LegIntent = LegIntent.UNPAVED) -> RouteRequest:
    return RouteRequest(
        waypoints=tuple(Coordinate(lat=lat, lon=lon) for lat, lon in points), intent=intent
    )


def north_of(point: tuple[float, float], metres: float) -> tuple[float, float]:
    """A point `metres` due north. One degree of latitude is ~111,320 m."""
    return (point[0] + metres / 111_320.0, point[1])


LEAVENWORTH = (47.5962, -120.6615)


class TestCoincidentSpan:
    """A leg whose two ends are the same place, which is how every loop trip begins.

    "Three days of dirt starting and ending in Leavenworth" makes the trip briefly
    [Leavenworth, Leavenworth] before the intermediate stops arrive. That is an ordinary
    transient state, not a mistake, so it must cost nothing and say nothing.
    """

    async def test_it_does_not_spend_a_request_on_a_zero_length_span(self):
        """The whole point: a metered call to be told what we already know."""
        inner = FakeProvider()
        await CoincidentSpanProvider(inner).route(span(LEAVENWORTH, LEAVENWORTH))
        assert inner.call_count == 0

    async def test_it_returns_a_leg_rather_than_raising(self):
        """A rider building a loop sees nothing, so nothing may escape as an error."""
        leg = await CoincidentSpanProvider(FakeProvider()).route(span(LEAVENWORTH, LEAVENWORTH))
        assert len(leg.geometry) == 2

    async def test_the_leg_is_honestly_two_coincident_points(self):
        """Not a fabricated line. Stitching collapses coincident boundaries already."""
        leg = await CoincidentSpanProvider(FakeProvider()).route(span(LEAVENWORTH, LEAVENWORTH))
        assert leg.geometry[0] == leg.geometry[1]
        assert leg.geometry[0] == Coordinate(lat=LEAVENWORTH[0], lon=LEAVENWORTH[1])

    async def test_it_reports_no_distance_and_no_duration(self):
        leg = await CoincidentSpanProvider(FakeProvider()).route(span(LEAVENWORTH, LEAVENWORTH))
        assert leg.distance_m == 0.0
        assert leg.duration_s == 0.0

    async def test_it_claims_no_surface_it_did_not_survey(self):
        leg = await CoincidentSpanProvider(FakeProvider()).route(span(LEAVENWORTH, LEAVENWORTH))
        assert leg.surface_spans == ()
        assert leg.ascent_m is None

    async def test_the_leg_is_attributed_to_the_engine_it_stood_in_for(self):
        """Otherwise a trip carries a leg whose provider nothing downstream recognizes."""
        inner = FakeProvider()
        leg = await CoincidentSpanProvider(inner).route(span(LEAVENWORTH, LEAVENWORTH))
        assert leg.provider == inner.capabilities.name

    async def test_it_preserves_the_requested_intent(self):
        leg = await CoincidentSpanProvider(FakeProvider()).route(
            span(LEAVENWORTH, LEAVENWORTH, intent=LegIntent.HIGHWAY_CONNECTOR)
        )
        assert leg.intent is LegIntent.HIGHWAY_CONNECTOR

    async def test_a_real_span_is_routed_normally(self):
        inner = FakeProvider()
        await CoincidentSpanProvider(inner).route(span(LEAVENWORTH, (47.7, -120.3)))
        assert inner.call_count == 1

    async def test_jitter_below_the_tolerance_is_the_same_point(self):
        """Rounding and float noise must not turn a loop into a metered request."""
        inner = FakeProvider()
        nudged = north_of(LEAVENWORTH, COINCIDENT_TOLERANCE_M / 2)
        await CoincidentSpanProvider(inner).route(span(LEAVENWORTH, nudged))
        assert inner.call_count == 0

    async def test_a_span_longer_than_the_tolerance_is_routed(self):
        """The guard must not swallow a short but real leg."""
        inner = FakeProvider()
        nudged = north_of(LEAVENWORTH, COINCIDENT_TOLERANCE_M * 10)
        await CoincidentSpanProvider(inner).route(span(LEAVENWORTH, nudged))
        assert inner.call_count == 1

    async def test_every_waypoint_must_coincide_not_just_the_ends(self):
        """A loop out to a via-point and back is a real route, not a zero-length span."""
        inner = FakeProvider()
        await CoincidentSpanProvider(inner).route(span(LEAVENWORTH, (47.7, -120.3), LEAVENWORTH))
        assert inner.call_count == 1

    async def test_more_than_two_coincident_waypoints_still_short_circuit(self):
        inner = FakeProvider()
        await CoincidentSpanProvider(inner).route(span(LEAVENWORTH, LEAVENWORTH, LEAVENWORTH))
        assert inner.call_count == 0
