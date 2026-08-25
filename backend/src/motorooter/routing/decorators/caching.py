"""In-memory LRU cache for routed legs.

Sized for the drag interaction: undo/redo and repeated drags over the same handle re-issue
byte-identical requests, and on a metered free tier those must not cost quota.
"""

from collections import OrderedDict
from dataclasses import dataclass

from motorooter.clock import Clock, SystemClock
from motorooter.routing.models import (
    COORDINATE_KEY_PRECISION,
    ProviderCapabilities,
    RouteLeg,
    RouteRequest,
)
from motorooter.routing.protocol import RoutingProvider

CacheKey = tuple[object, ...]


@dataclass(frozen=True)
class _Entry:
    leg: RouteLeg
    stored_at: float


class CachingProvider:
    """Caches successful legs. Failures are never cached."""

    def __init__(
        self,
        inner: RoutingProvider,
        *,
        clock: Clock | None = None,
        ttl_s: float | None = None,
        max_entries: int = 512,
        precision: int = COORDINATE_KEY_PRECISION,
    ) -> None:
        """
        Args:
            inner: provider to wrap.
            clock: time source; injected so TTL tests need no real waiting.
            ttl_s: entry lifetime. `None` keeps entries until evicted.
            max_entries: LRU capacity.
            precision: decimal places coordinates are rounded to for the key. Shared with
                `RouteFingerprint` so the two agree on what counts as the same request.
        """
        self._inner = inner
        self._clock = clock or SystemClock()
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._precision = precision
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    def _key(self, request: RouteRequest) -> CacheKey:
        return (
            tuple(
                (round(wp.lat, self._precision), round(wp.lon, self._precision))
                for wp in request.waypoints
            ),
            request.intent,
            request.avoid_highways,
            request.avoid_tolls,
            request.avoid_ferries,
            request.want_elevation,
        )

    def _expired(self, entry: _Entry) -> bool:
        return self._ttl_s is not None and self._clock.now() - entry.stored_at > self._ttl_s

    async def route(self, request: RouteRequest) -> RouteLeg:
        key = self._key(request)
        entry = self._entries.get(key)
        if entry is not None and not self._expired(entry):
            self._entries.move_to_end(key)
            self.hits += 1
            return entry.leg

        self.misses += 1
        leg = await self._inner.route(request)  # errors propagate uncached, by design
        self._entries[key] = _Entry(leg=leg, stored_at=self._clock.now())
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return leg
