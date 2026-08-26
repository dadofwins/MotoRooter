"""The single mapping from exception to HTTP response.

The `ErrorCode` vocabulary itself lives in `motorooter.error_codes`, which imports nothing —
this module imports `trips.errors` and `routing.errors` to build the table, so the enum has
to sit below both to stay usable from a domain model. It is re-exported here so existing
imports and `schemas.py` are unaffected.

`ErrorCode` is re-exported (explicitly, so type checkers treat it as public) because
`schemas.py` and every existing caller import it from here.

Adding an error means adding it to the table below. `test_error_codes.py` fails if an
exception the API can raise is missing, so the two cannot drift apart.
"""

from motorooter.api.errors import NotImplementedYet, PlaceNotDisplayable
from motorooter.error_codes import ErrorCode as ErrorCode
from motorooter.llm.errors import LlmQuotaExceeded, LlmRefused, LlmUnavailable, ToolCallFailed
from motorooter.planning.discovery.errors import (
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.routing.errors import (
    InvalidRequest,
    NoRouteFound,
    ProviderNotFound,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
    RouteIncomplete,
    RoutingConfigError,
    UnsupportedIntent,
)
from motorooter.trips.errors import (
    TripAlreadyExists,
    TripDocumentInvalid,
    TripModifiedConcurrently,
    TripNotFound,
    TripStorageConfigError,
    TripStorageUnavailable,
)
from motorooter.trips.slug import InvalidSlug

ERROR_TABLE: dict[type[Exception], tuple[int, ErrorCode]] = {
    # Routing
    InvalidRequest: (400, ErrorCode.INVALID_REQUEST),
    UnsupportedIntent: (400, ErrorCode.UNSUPPORTED_INTENT),
    ProviderNotFound: (404, ErrorCode.PROVIDER_NOT_FOUND),
    NoRouteFound: (422, ErrorCode.NO_ROUTE_FOUND),
    # The trip exists but is not fully or freshly routed. Client-actionable: press Replan.
    RouteIncomplete: (422, ErrorCode.ROUTE_INCOMPLETE),
    QuotaExceeded: (429, ErrorCode.QUOTA_EXCEEDED),
    # Also 429, but a distinct code: the client should retry this one shortly and must not
    # retry the other. Same status, opposite advice.
    RateLimited: (429, ErrorCode.RATE_LIMITED),
    ProviderUnavailable: (502, ErrorCode.PROVIDER_UNAVAILABLE),
    LlmUnavailable: (502, ErrorCode.LLM_UNAVAILABLE),
    LlmQuotaExceeded: (429, ErrorCode.LLM_QUOTA_EXCEEDED),
    LlmRefused: (502, ErrorCode.LLM_REFUSED),
    ToolCallFailed: (500, ErrorCode.TOOL_CALL_FAILED),
    # Discovery. Search, Places and the categoriser all surface these, and none of them was
    # mapped — every one escaped as an untyped 500 with no code, which is how a rider met
    # "detail for this place could not be loaded" on a place Google describes well.
    DiscoveryRateLimited: (429, ErrorCode.DISCOVERY_RATE_LIMITED),
    DiscoveryQuotaExceeded: (429, ErrorCode.DISCOVERY_QUOTA_EXCEEDED),
    # A provider rejection — bad key, malformed query, blocked — so ours to fix, not the
    # client's to retry.
    DiscoveryRefused: (502, ErrorCode.DISCOVERY_REFUSED),
    DiscoveryUnavailable: (502, ErrorCode.DISCOVERY_UNAVAILABLE),
    # No entry for the `DiscoveryError` base, deliberately. A fallback would answer 502 for
    # a subclass nobody had classified, which is the drift the guard above exists to make
    # loud — and it would have to share a wire code with one of the specifics, which clients
    # switch on.
    # API surface
    PlaceNotDisplayable: (422, ErrorCode.PLACE_NOT_DISPLAYABLE),
    # Trips
    InvalidSlug: (400, ErrorCode.INVALID_SLUG),
    TripNotFound: (404, ErrorCode.TRIP_NOT_FOUND),
    TripAlreadyExists: (409, ErrorCode.TRIP_ALREADY_EXISTS),
    # Also 409, but distinct from TRIP_ALREADY_EXISTS on the wire: one means "choose
    # another name", this one means "re-read, merge again, and retry".
    TripModifiedConcurrently: (409, ErrorCode.TRIP_MODIFIED_CONCURRENTLY),
    # A stored document that will not parse, or was written by a newer schema version.
    # Server-side data corruption, so 500 — but a distinct code, because "your trip is
    # unreadable" is worth telling a client apart from "something broke".
    TripDocumentInvalid: (500, ErrorCode.TRIP_DOCUMENT_INVALID),
    TripStorageUnavailable: (503, ErrorCode.TRIP_STORAGE_UNAVAILABLE),
    # API surface
    NotImplementedYet: (501, ErrorCode.NOT_IMPLEMENTED),
}
"""Exception type -> (HTTP status, wire code).

`RoutingConfigError` and `TripStorageConfigError` are deliberately absent: both are raised
at startup so a misconfigured deploy fails rather than serving, and neither can reach a
request handler.
"""

STARTUP_ONLY: frozenset[type[Exception]] = frozenset({RoutingConfigError, TripStorageConfigError})
"""Exceptions that fail the deploy instead of a request. Asserted by the drift test."""


def resolve(exc: Exception) -> tuple[int, ErrorCode]:
    """Status and code for an exception.

    Walks the MRO so a subclass added later inherits its parent's mapping rather than
    silently falling through. An unmapped exception is a gap in `ERROR_TABLE`, so it
    answers 500 — a server bug, not something to blame on the client.
    """
    for cls in type(exc).__mro__:
        if cls in ERROR_TABLE:
            return ERROR_TABLE[cls]
    return 500, ErrorCode.INTERNAL_ERROR
