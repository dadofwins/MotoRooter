"""Two limits, two windows, two meanings.

ORS caps requests per day *and* per minute. Only the daily cap was enforced, so a burst
inside one minute sailed past the local guard and came back as an opaque upstream failure —
which is exactly how it was found: fifty requests fired while verifying something else, and
the resulting wave of errors read as "the fix did not work".

The distinction that matters is what a caller should do next. A daily cap is final: waiting
until tomorrow is not a retry strategy, so it must not be retried. A per-minute cap clears in
under a minute, so backing off is precisely the right response. Collapsing both into one
error makes one of those two behaviours wrong.
"""

import pytest
import respx

from motorooter.clock import FakeClock
from motorooter.routing.decorators.quota import (
    SECONDS_PER_DAY,
    SECONDS_PER_MINUTE,
    QuotaGuardProvider,
)
from motorooter.routing.decorators.retry import RetryingProvider
from motorooter.routing.errors import QuotaExceeded, RateLimited
from motorooter.routing.factory import RoutingSettings, build_routing
from motorooter.routing.models import Coordinate, LegIntent, ProviderCapabilities, RouteRequest
from motorooter.routing.providers.fake import FakeProvider
from tests.routing.test_ors import DIRECTIONS_URL, echo_requested_coordinates


def request() -> RouteRequest:
    return RouteRequest(
        waypoints=(Coordinate(lat=45.0, lon=-121.0), Coordinate(lat=46.0, lon=-121.0)),
        intent=LegIntent.UNPAVED,
    )


def provider(*, daily: int | None = 2000, per_minute: int | None = 40) -> FakeProvider:
    return FakeProvider(
        capabilities=ProviderCapabilities(
            name="ors", daily_quota=daily, per_minute_quota=per_minute
        )
    )


class TestTheTwoLimitsAreDifferent:
    def test_a_daily_cap_is_not_retryable(self):
        """Waiting until tomorrow is not a retry strategy."""
        assert QuotaExceeded("x").retryable is False

    def test_a_rate_limit_is_retryable(self):
        """It clears in under a minute; backing off is the correct response."""
        assert RateLimited("x").retryable is True

    def test_a_rate_limit_is_not_a_quota_exhaustion(self):
        assert not isinstance(RateLimited("x"), QuotaExceeded)


class TestThePerMinuteGuard:
    async def test_it_allows_calls_under_the_cap(self):
        clock = FakeClock()
        guard = QuotaGuardProvider(
            provider(),
            clock=clock,
            limit=3,
            window_s=SECONDS_PER_MINUTE,
            window_name="per-minute",
            exhausted=RateLimited,
        )
        for _ in range(3):
            await guard.route(request())
        assert guard.used == 3

    async def test_it_refuses_past_the_cap(self):
        clock = FakeClock()
        guard = QuotaGuardProvider(
            provider(),
            clock=clock,
            limit=2,
            window_s=SECONDS_PER_MINUTE,
            window_name="per-minute",
            exhausted=RateLimited,
        )
        await guard.route(request())
        await guard.route(request())
        with pytest.raises(RateLimited):
            await guard.route(request())

    async def test_the_message_names_the_window_it_is_talking_about(self):
        """Stacked guards otherwise both report "daily", which is actively misleading."""
        clock = FakeClock()
        guard = QuotaGuardProvider(
            provider(),
            clock=clock,
            limit=1,
            window_s=SECONDS_PER_MINUTE,
            window_name="per-minute",
            exhausted=RateLimited,
        )
        await guard.route(request())
        with pytest.raises(RateLimited, match="per-minute"):
            await guard.route(request())

    async def test_the_window_rolls(self):
        clock = FakeClock()
        guard = QuotaGuardProvider(
            provider(),
            clock=clock,
            limit=1,
            window_s=SECONDS_PER_MINUTE,
            window_name="per-minute",
            exhausted=RateLimited,
        )
        await guard.route(request())
        clock.advance(SECONDS_PER_MINUTE + 1)
        await guard.route(request())
        assert guard.used == 1

    async def test_it_defaults_to_the_declared_per_minute_quota(self):
        guard = QuotaGuardProvider(
            provider(per_minute=2),
            clock=FakeClock(),
            window_s=SECONDS_PER_MINUTE,
            window_name="per-minute",
            exhausted=RateLimited,
        )
        await guard.route(request())
        await guard.route(request())
        with pytest.raises(RateLimited):
            await guard.route(request())


class TestTheDailyGuardIsUnchanged:
    async def test_it_still_defaults_to_the_daily_quota(self):
        guard = QuotaGuardProvider(provider(daily=2), clock=FakeClock())
        await guard.route(request())
        await guard.route(request())
        with pytest.raises(QuotaExceeded):
            await guard.route(request())

    async def test_its_window_is_still_a_day(self):
        clock = FakeClock()
        guard = QuotaGuardProvider(provider(daily=1), clock=clock)
        await guard.route(request())
        clock.advance(SECONDS_PER_DAY - 1)
        with pytest.raises(QuotaExceeded):
            await guard.route(request())


class TestRetryBehavesDifferentlyForEach:
    """The reason the two errors exist at all."""

    async def test_a_rate_limited_upstream_is_retried(self):
        inner = FakeProvider(error=RateLimited("slow down", provider="ors"), fail_first=2)
        clock = FakeClock()
        leg = await RetryingProvider(inner, clock=clock, attempts=4).route(request())
        assert leg.provider == "fake"
        assert inner.call_count == 3

    async def test_an_exhausted_daily_quota_is_not_retried(self):
        """Retrying spends nothing and gains nothing; it just delays the error."""
        inner = FakeProvider(error=QuotaExceeded("done for today", provider="ors"))
        with pytest.raises(QuotaExceeded):
            await RetryingProvider(inner, clock=FakeClock(), attempts=4).route(request())
        assert inner.call_count == 1


class TestTheFactoryComposesBothGuards:
    """The gap a mutation test found: nothing checked that the stack is actually built.

    Every piece existed — the error, the capability, the generalised guard — and the wiring
    that connects them was unverified. A decorator nobody composes is a decorator that does
    not run.
    """

    @pytest.fixture
    def mock_ors(self):
        """Hermetic. Without this the built adapter reaches the real ORS — which it did on
        the first run of these tests, and answered 403."""
        with respx.mock(assert_all_called=False) as mock:
            mock.route(DIRECTIONS_URL).mock(side_effect=echo_requested_coordinates)
            yield mock

    @staticmethod
    def _ors(settings: RoutingSettings, clock: FakeClock):
        registry, _ = build_routing(settings, clock=clock)
        return registry.get("ors")

    @pytest.fixture
    def live(self):
        return RoutingSettings(ors_api_key="k", google_api_key="k")

    async def test_the_per_minute_ceiling_is_enforced_on_the_built_stack(self, live, mock_ors):
        """Fifty requests inside a minute is what a discovery fan-out looks like."""
        clock = FakeClock()
        ors = self._ors(live, clock)
        with pytest.raises(RateLimited):
            for index in range(60):
                # Distinct coordinates, so the caching decorator cannot absorb them.
                await ors.route(
                    RouteRequest(
                        waypoints=(
                            Coordinate(lat=45.0 + index * 0.01, lon=-121.0),
                            Coordinate(lat=46.0, lon=-121.0),
                        ),
                        intent=LegIntent.UNPAVED,
                    )
                )

    async def test_the_minute_window_clears(self, live, mock_ors):
        """It is a rate limit, not a budget: waiting is supposed to work."""
        clock = FakeClock()
        ors = self._ors(live, clock)

        async def burst(offset: int) -> int:
            sent = 0
            for index in range(60):
                try:
                    await ors.route(
                        RouteRequest(
                            waypoints=(
                                Coordinate(lat=45.0 + (offset + index) * 0.01, lon=-121.0),
                                Coordinate(lat=46.0, lon=-121.0),
                            ),
                            intent=LegIntent.UNPAVED,
                        )
                    )
                    sent += 1
                except RateLimited:
                    break
            return sent

        assert await burst(0) > 0
        clock.advance(SECONDS_PER_MINUTE + 1)
        assert await burst(100) > 0

    def test_a_self_hosted_instance_has_neither_ceiling(self):
        """Own hardware, own limits. The hosted caps must not follow it there."""
        registry, _ = build_routing(
            RoutingSettings(
                ors_api_key="k", google_api_key="k", ors_base_url="http://localhost:8080/ors"
            ),
            clock=FakeClock(),
        )
        capabilities = registry.get("ors").capabilities
        assert capabilities.daily_quota is None
        assert capabilities.per_minute_quota is None
