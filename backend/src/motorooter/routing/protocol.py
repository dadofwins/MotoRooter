"""The one interface every routing engine implements.

Deliberately narrow: two members. A narrow protocol is what makes `FakeProvider` trivial
(and therefore the test suite hermetic) and what lets caching/retry/quota be written once
as decorators rather than per adapter.
"""

from typing import Protocol, runtime_checkable

from motorooter.routing.models import ProviderCapabilities, RouteLeg, RouteRequest


@runtime_checkable
class RoutingProvider(Protocol):
    """A routing engine, or a decorator wrapping one.

    Implementations must raise only `RoutingError` subclasses from `route`, so callers
    never need to catch provider-specific exceptions.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Static description of what this provider supports and what it costs."""
        ...

    async def route(self, request: RouteRequest) -> RouteLeg:
        """Route a single leg.

        Raises:
            InvalidRequest: input the engine cannot route from.
            NoRouteFound: engine answered that no route exists.
            QuotaExceeded: rate limit or daily cap reached.
            ProviderUnavailable: transient upstream failure.
        """
        ...
