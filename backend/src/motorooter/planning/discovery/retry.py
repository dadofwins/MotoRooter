"""Retry with backoff, for the discovery stack.

Root CLAUDE.md asked for caching, retry and quota here from the beginning, and discovery fans
out far more requests per user action than routing does. Only caching has fallen away — Brave
forbids storing search results — so the value of this layer is entirely in surviving a 429
rather than in avoiding one.

Modelled on `routing.decorators.retry`, and differing in one way that matters: Brave says when
to come back. `x-ratelimit-reset` is seconds until the window resets, and on our key that
window is one second. Guessing exponentially would sleep an order of magnitude too long, so
the header is honoured when present and the guess is the fallback.
"""

from motorooter.clock import Clock, SystemClock
from motorooter.planning.discovery.errors import DiscoveryError, DiscoveryRateLimited
from motorooter.planning.discovery.models import Candidate
from motorooter.planning.discovery.protocol import DEFAULT_RESULT_LIMIT, SearchSource
from motorooter.planning.discovery.queries import SearchQuery
from motorooter.routing.models import Coordinate


class RetryingSearchSource:
    """Re-attempts searches that failed with a retryable error.

    Dispatches on `DiscoveryError.retryable`, so a spent budget or a refused request fails at
    once. That split is the whole safety property: `DiscoveryRateLimited` means come back
    shortly, `DiscoveryQuotaExceeded` means the money is gone, and retrying the second turns
    an outage into a storm indistinguishable from the thing it is chasing.
    """

    def __init__(
        self,
        inner: SearchSource,
        *,
        clock: Clock | None = None,
        attempts: int = 3,
        backoff_s: float = 0.5,
        max_backoff_s: float = 8.0,
    ) -> None:
        """
        Args:
            inner: source to wrap.
            clock: time source; injected so backoff tests assert a schedule, not a wait.
            attempts: total tries including the first.
            backoff_s: delay before the second attempt when upstream did not say; doubles.
            max_backoff_s: ceiling, applied to the guess *and* to what upstream asked for.
        """
        if attempts < 1:
            msg = f"attempts must be at least 1, got {attempts}"
            raise ValueError(msg)
        self._inner = inner
        self._clock = clock or SystemClock()
        self._attempts = attempts
        self._backoff_s = backoff_s
        self._max_backoff_s = max_backoff_s

    @property
    def name(self) -> str:
        """The wrapped source's name. Candidates carry it as provenance, and a decorator
        that renamed the source would misattribute every result."""
        return self._inner.name

    async def search(
        self,
        query: SearchQuery,
        *,
        near: Coordinate,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> tuple[Candidate, ...]:
        guess = self._backoff_s
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._inner.search(query, near=near, limit=limit)
            except DiscoveryError as exc:
                if not exc.retryable or attempt == self._attempts:
                    raise
                await self._clock.sleep(self._delay(exc, guess))
                guess = min(guess * 2, self._max_backoff_s)
        raise AssertionError("unreachable: loop either returns or raises")  # pragma: no cover

    def _delay(self, exc: DiscoveryError, guess: float) -> float:
        """How long to wait: what upstream asked for, or the guess.

        Capped either way. A retry-after is upstream input, and sleeping for an hour because
        a server said so would hang a rider's replan on a number nobody validated — the same
        reason a rating of 7 is dropped rather than clamped, applied to a delay that can only
        be too long rather than wrong.
        """
        asked = (
            getattr(exc, "retry_after_s", None) if isinstance(exc, DiscoveryRateLimited) else None
        )
        if isinstance(asked, int | float) and asked > 0:
            return min(float(asked), self._max_backoff_s)
        return min(guess, self._max_backoff_s)
