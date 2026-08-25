"""Client-side quota accounting over a fixed window.

Free routing tiers are small enough that the drag interaction can exhaust one in a single
session. Rejecting locally keeps the failure fast and legible, and leaves headroom rather
than discovering the ceiling as an opaque upstream 429.

One window per instance, and providers impose more than one — ORS caps both per day and per
minute. Stack an instance per window rather than trying to express both in one: they differ
in length, in limit, and in what the caller should do when they are hit.
"""

from motorooter.clock import Clock, SystemClock
from motorooter.routing.errors import QuotaExceeded, RoutingConfigError, RoutingError
from motorooter.routing.models import ProviderCapabilities, RouteLeg, RouteRequest
from motorooter.routing.protocol import RoutingProvider

SECONDS_PER_DAY = 86_400.0
SECONDS_PER_MINUTE = 60.0


class QuotaGuardProvider:
    """Counts upstream calls in a rolling fixed window and refuses once the cap is hit.

    Counts attempts rather than successes: a failed call is still a billed request.
    """

    def __init__(
        self,
        inner: RoutingProvider,
        *,
        clock: Clock | None = None,
        limit: int | None = None,
        window_s: float = SECONDS_PER_DAY,
        window_name: str = "daily",
        exhausted: type[RoutingError] = QuotaExceeded,
    ) -> None:
        """
        Args:
            inner: provider to wrap.
            clock: time source; injected so window-reset tests need no real waiting.
            limit: cap per window. Defaults to the provider's declared quota for this
                window; required if the provider declares none.
            window_s: window length. The window starts at the first call and resets once
                elapsed, which is a deliberate simplification over the provider's true
                UTC-midnight reset — it is conservative, never over-permissive.
            window_name: how the window is described when the cap is hit. Stacked guards
                otherwise both report "daily", which is actively misleading.
            exhausted: error raised at the cap. `QuotaExceeded` for a period budget,
                `RateLimited` for a short window — the difference decides whether a caller
                backs off or gives up, so the local guard raises whichever the upstream
                would have.
        """
        declared = (
            inner.capabilities.per_minute_quota
            if window_s <= SECONDS_PER_MINUTE
            else inner.capabilities.daily_quota
        )
        limit = limit if limit is not None else declared
        if limit is None:
            msg = (
                f"provider {inner.capabilities.name!r} declares no {window_name} quota; "
                "pass limit explicitly"
            )
            raise RoutingConfigError(msg)
        self._inner = inner
        self._clock = clock or SystemClock()
        self._limit = limit
        self._window_s = window_s
        self._window_name = window_name
        self._exhausted = exhausted
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
            msg = f"{self._window_name} quota of {self._limit} requests exhausted"
            raise self._exhausted(msg, provider=self.capabilities.name)
        self._used += 1  # charged before dispatch: a failed call still costs
        return await self._inner.route(request)
