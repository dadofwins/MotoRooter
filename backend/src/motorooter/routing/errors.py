"""Provider-neutral routing errors.

Adapters translate their upstream's failure modes into these so that retry, quota, and
fallback logic never needs to know which engine produced the failure.
"""


class RoutingError(Exception):
    """Base for all routing failures."""

    retryable: bool = False

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}" if provider else message)


class ProviderUnavailable(RoutingError):
    """Transient upstream failure: timeout, 5xx, connection reset."""

    retryable = True


class QuotaExceeded(RoutingError):
    """The budget for the period is spent. Not retryable — waiting for tomorrow is not a
    retry strategy, and trying again only delays the same answer."""


class RateLimited(RoutingError):
    """Too many requests too quickly. Retryable, because the window clears in seconds.

    Deliberately not a subclass of `QuotaExceeded`: they arrive from the same provider and
    often the same status code, but a caller should back off on one and give up on the
    other. Folding them together makes one of those two behaviours wrong.
    """

    retryable = True


class NoRouteFound(RoutingError):
    """The engine answered successfully that no route exists. Deterministic, so final."""


class InvalidRequest(RoutingError):
    """Malformed or unroutable input, e.g. a waypoint with no road within snapping range."""


class ProviderNotFound(RoutingError):
    """A provider was requested by name and is not registered."""


class UnsupportedIntent(RoutingError):
    """No policy is configured for a leg intent."""


class RouteIncomplete(RoutingError):
    """A trip was asked for a continuous route it cannot currently produce.

    Either a leg has no geometry, or its geometry is stale — produced under an intent or
    provider the leg no longer carries, because a re-route failed and the previous result
    was kept. Both are refused rather than stitched: a route missing or misrepresenting a
    section still renders perfectly, so nothing downstream would catch it.
    """

    def __init__(self, leg_indices: tuple[int, ...], *, reason: str = "unrouted") -> None:
        self.leg_indices = leg_indices
        self.reason = reason
        listed = ", ".join(str(index) for index in leg_indices)
        super().__init__(f"trip has {reason} legs at index {listed}")


class RoutingConfigError(RoutingError):
    """Wiring is inconsistent — raised at startup, never mid-request.

    Catching these at construction is deliberate: a policy pointing at a paved-only engine
    for dirt legs should fail the deploy, not silently produce bad routes.
    """
