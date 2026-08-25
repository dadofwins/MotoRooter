"""Retry with exponential backoff, for transient upstream failures only."""

from motorooter.clock import Clock, SystemClock
from motorooter.routing.errors import RoutingConfigError, RoutingError
from motorooter.routing.models import ProviderCapabilities, RouteLeg, RouteRequest
from motorooter.routing.protocol import RoutingProvider


class RetryingProvider:
    """Re-attempts calls that failed with a retryable error.

    Dispatches on `RoutingError.retryable`, so a definitive answer (no route found) or a
    budget rejection (quota exceeded) fails immediately rather than burning attempts.
    """

    def __init__(
        self,
        inner: RoutingProvider,
        *,
        clock: Clock | None = None,
        attempts: int = 3,
        backoff_s: float = 0.25,
        max_backoff_s: float = 8.0,
    ) -> None:
        """
        Args:
            inner: provider to wrap.
            clock: time source; injected so backoff tests assert a schedule, not a wait.
            attempts: total tries including the first. Must be >= 1.
            backoff_s: delay before the second attempt; doubles thereafter.
            max_backoff_s: ceiling on the doubling.
        """
        if attempts < 1:
            msg = f"attempts must be at least 1, got {attempts}"
            raise RoutingConfigError(msg)
        self._inner = inner
        self._clock = clock or SystemClock()
        self._attempts = attempts
        self._backoff_s = backoff_s
        self._max_backoff_s = max_backoff_s

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    async def route(self, request: RouteRequest) -> RouteLeg:
        delay = self._backoff_s
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._inner.route(request)
            except RoutingError as exc:
                if not exc.retryable or attempt == self._attempts:
                    raise
                await self._clock.sleep(delay)
                delay = min(delay * 2, self._max_backoff_s)
        raise AssertionError("unreachable: loop either returns or raises")  # pragma: no cover
