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
    """Rate limit or daily cap. Not retryable — retrying spends the remaining budget."""


class NoRouteFound(RoutingError):
    """The engine answered successfully that no route exists. Deterministic, so final."""


class InvalidRequest(RoutingError):
    """Malformed or unroutable input, e.g. a waypoint with no road within snapping range."""


class ProviderNotFound(RoutingError):
    """A provider was requested by name and is not registered."""


class UnsupportedIntent(RoutingError):
    """No policy is configured for a leg intent."""


class RoutingConfigError(RoutingError):
    """Wiring is inconsistent — raised at startup, never mid-request.

    Catching these at construction is deliberate: a policy pointing at a paved-only engine
    for dirt legs should fail the deploy, not silently produce bad routes.
    """
