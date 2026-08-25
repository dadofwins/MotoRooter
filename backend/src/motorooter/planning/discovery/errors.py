"""Discovery failures, source-neutral.

Same shape as `routing.errors` and `llm.errors`: adapters translate their upstream's failure
modes into these so retry and quota logic never needs to know which vendor failed.

The rate-limited/quota-exhausted split is deliberate and was learned the hard way in the
routing layer — a caller should back off on one and give up on the other, and folding them
together makes one of those behaviours wrong.
"""


class DiscoveryError(Exception):
    """Base for discovery failures."""

    retryable: bool = False


class DiscoveryUnavailable(DiscoveryError):
    """Transient upstream failure: timeout, 5xx, connection reset."""

    retryable = True


class DiscoveryRateLimited(DiscoveryError):
    """Too many requests too quickly. Clears in seconds, so retrying is correct."""

    retryable = True


class DiscoveryQuotaExceeded(DiscoveryError):
    """The search budget is spent. Retrying gains nothing but delay."""


class DiscoveryRefused(DiscoveryError):
    """The provider rejected the request: bad key, malformed query, blocked."""
