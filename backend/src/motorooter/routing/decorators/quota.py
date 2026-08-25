"""Client-side daily quota accounting.

Free routing tiers are small enough that the drag interaction can exhaust one in a single
session. Rejecting locally keeps the failure fast and legible, and leaves headroom rather
than discovering the ceiling as an opaque upstream 429.
"""

from motorooter.clock import Clock, SystemClock
from motorooter.routing.errors import QuotaExceeded, RoutingConfigError
from motorooter.routing.models import ProviderCapabilities, RouteLeg, RouteRequest
from motorooter.routing.protocol import RoutingProvider

SECONDS_PER_DAY = 86_400.0


class QuotaGuardProvider:
    """Counts upstream calls in a rolling fixed window and refuses once the cap is hit.

    Counts attempts rather than successes: a failed call is still a billed request.
    """

    def __init__(
        self,
        inner: RoutingProvider,
        *,
        clock: Clock | None = None,
        daily_limit: int | None = None,
        window_s: float = SECONDS_PER_DAY,
    ) -> None:
        """
        Args:
            inner: provider to wrap.
            clock: time source; injected so window-reset tests need no real waiting.
            daily_limit: cap per window. Defaults to the provider's declared
                `capabilities.daily_quota`; required if the provider declares none.
            window_s: window length. The window starts at the first call and resets once
                elapsed, which is a deliberate simplification over the provider's true
                UTC-midnight reset — it is conservative, never over-permissive.
        """
        limit = daily_limit if daily_limit is not None else inner.capabilities.daily_quota
        if limit is None:
            msg = (
                f"provider {inner.capabilities.name!r} declares no daily_quota; "
                "pass daily_limit explicitly"
            )
            raise RoutingConfigError(msg)
        self._inner = inner
        self._clock = clock or SystemClock()
        self._limit = limit
        self._window_s = window_s
        self._used = 0
        self._window_start = clock.now() if clock else SystemClock().now()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    def _roll_window(self) -> None:
        if self._clock.now() - self._window_start >= self._window_s:
            self._window_start = self._clock.now()
            self._used = 0

    @property
    def used(self) -> int:
        self._roll_window()
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self._limit - self.used)

    async def route(self, request: RouteRequest) -> RouteLeg:
        self._roll_window()
        if self._used >= self._limit:
            msg = f"daily quota of {self._limit} requests exhausted"
            raise QuotaExceeded(msg, provider=self.capabilities.name)
        self._used += 1  # charged before dispatch: a failed call still costs
        return await self._inner.route(request)
