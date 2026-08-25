"""Error taxonomy.

The retry decorator dispatches on `retryable`, so misclassifying an error here either
wastes quota retrying a permanent failure or gives up on a transient one.
"""

from motorooter.routing.errors import (
    InvalidRequest,
    NoRouteFound,
    ProviderUnavailable,
    QuotaExceeded,
    RoutingError,
)


def test_all_errors_share_a_base():
    for cls in (ProviderUnavailable, QuotaExceeded, NoRouteFound, InvalidRequest):
        assert issubclass(cls, RoutingError)


def test_provider_unavailable_is_retryable():
    assert ProviderUnavailable("502 from upstream").retryable is True


def test_quota_exceeded_is_not_retryable():
    """Retrying a quota rejection burns the remaining budget faster."""
    assert QuotaExceeded("daily cap hit").retryable is False


def test_no_route_found_is_not_retryable():
    """A genuine no-route answer is deterministic; retrying returns the same answer."""
    assert NoRouteFound("no path between waypoints").retryable is False


def test_invalid_request_is_not_retryable():
    assert InvalidRequest("waypoint in the ocean").retryable is False


def test_error_carries_provider_name():
    err = ProviderUnavailable("timeout", provider="ors")
    assert err.provider == "ors"
    assert "ors" in str(err)
