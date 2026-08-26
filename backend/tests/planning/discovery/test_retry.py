"""Retry for the discovery stack, which had none.

Root CLAUDE.md called for caching, retry and quota here from the start, and discovery fans
out far more requests per user action than routing does. Only the shape has changed since:
Brave forbids caching results, so retry and quota are what this layer gets, and the value is
entirely in surviving a 429 rather than in avoiding one.

Two things make this different from the routing retry it is modelled on.

**Brave says when to come back.** `x-ratelimit-reset` is seconds until the window resets,
measured at 1 on our key. Exponential backoff would sleep far longer than needed on a
per-second window, so the header is read and the guess is the fallback.

**A spent budget is not a slow moment.** `DiscoveryRateLimited` means try again shortly;
`DiscoveryQuotaExceeded` means the money is gone. Retrying the second turns an outage into a
storm that looks exactly like the thing it is retrying.
"""

import pytest

from motorooter.clock import FakeClock
from motorooter.planning.discovery.errors import (
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.planning.discovery.models import Candidate
from motorooter.planning.discovery.queries import SearchQuery
from motorooter.planning.discovery.retry import RetryingSearchSource
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory

ANCHOR = Coordinate(lat=46.87, lon=-121.52)
QUERY = SearchQuery(
    text="wild camping chinook pass", place="Chinook Pass", category=PoiCategory.WILD_CAMP
)


class Flaky:
    """Fails a scripted number of times, then succeeds."""

    name = "flaky"

    def __init__(self, *errors: Exception):
        self.errors = list(errors)
        self.calls = 0

    async def search(self, query, *, near, limit=5):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return (
            Candidate(
                name="Halfway Flat",
                category=query.category,
                found_near=near,
                source=self.name,
            ),
        )


def retrying(source, **overrides):
    clock = overrides.pop("clock", FakeClock())
    return RetryingSearchSource(source, clock=clock, **overrides), clock


class TestItSurvivesATransientFailure:
    async def test_a_rate_limit_is_retried(self):
        source = Flaky(DiscoveryRateLimited("slow down"))
        wrapped, _ = retrying(source)
        assert await wrapped.search(QUERY, near=ANCHOR)
        assert source.calls == 2

    async def test_an_outage_is_retried(self):
        source = Flaky(DiscoveryUnavailable("upstream down"))
        wrapped, _ = retrying(source)
        assert await wrapped.search(QUERY, near=ANCHOR)

    async def test_it_gives_up_after_the_last_attempt(self):
        source = Flaky(*[DiscoveryRateLimited("slow down")] * 5)
        wrapped, _ = retrying(source, attempts=3)
        with pytest.raises(DiscoveryRateLimited):
            await wrapped.search(QUERY, near=ANCHOR)
        assert source.calls == 3

    async def test_the_source_name_is_passed_through(self):
        """A decorator that renamed the source would break candidate provenance."""
        wrapped, _ = retrying(Flaky())
        assert wrapped.name == "flaky"


class TestWhatIsNotRetried:
    async def test_a_spent_budget_fails_immediately(self):
        """Retrying an exhausted budget is a storm that looks like the outage it is chasing."""
        source = Flaky(DiscoveryQuotaExceeded("budget spent"))
        wrapped, _ = retrying(source)
        with pytest.raises(DiscoveryQuotaExceeded):
            await wrapped.search(QUERY, near=ANCHOR)
        assert source.calls == 1

    async def test_a_refusal_fails_immediately(self):
        """A bad key or a malformed request will refuse identically forever."""
        source = Flaky(DiscoveryRefused("bad key"))
        wrapped, _ = retrying(source)
        with pytest.raises(DiscoveryRefused):
            await wrapped.search(QUERY, near=ANCHOR)
        assert source.calls == 1


class TestHowLongItWaits:
    async def test_it_honours_the_reset_header_when_given_one(self):
        """Brave returns `x-ratelimit-reset` in seconds. Measured at 1 on our key, against a
        first exponential guess an order of magnitude larger."""
        source = Flaky(DiscoveryRateLimited("slow down", retry_after_s=1.0))
        wrapped, clock = retrying(source, backoff_s=10.0)
        await wrapped.search(QUERY, near=ANCHOR)
        assert clock.slept == [1.0]

    async def test_it_guesses_when_there_is_no_header(self):
        source = Flaky(DiscoveryUnavailable("upstream down"))
        wrapped, clock = retrying(source, backoff_s=0.25)
        await wrapped.search(QUERY, near=ANCHOR)
        assert clock.slept == [0.25]

    async def test_the_guess_doubles(self):
        source = Flaky(*[DiscoveryUnavailable("down")] * 2)
        wrapped, clock = retrying(source, attempts=4, backoff_s=0.25)
        await wrapped.search(QUERY, near=ANCHOR)
        assert clock.slept == [0.25, 0.5]

    async def test_the_guess_is_capped(self):
        source = Flaky(*[DiscoveryUnavailable("down")] * 3)
        wrapped, clock = retrying(source, attempts=5, backoff_s=1.0, max_backoff_s=1.5)
        await wrapped.search(QUERY, near=ANCHOR)
        assert clock.slept == [1.0, 1.5, 1.5]

    async def test_an_absurd_reset_is_not_obeyed(self):
        """A header is upstream input. Sleeping for an hour because a server said so would
        hang a rider's replan on a number nobody validated."""
        source = Flaky(DiscoveryRateLimited("slow down", retry_after_s=3600.0))
        wrapped, clock = retrying(source, max_backoff_s=8.0)
        await wrapped.search(QUERY, near=ANCHOR)
        assert clock.slept == [8.0]

    async def test_it_does_not_sleep_when_it_succeeds(self):
        wrapped, clock = retrying(Flaky())
        await wrapped.search(QUERY, near=ANCHOR)
        assert clock.slept == []
