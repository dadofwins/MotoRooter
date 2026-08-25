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

from motorooter.api.errors import NotImplementedYet
from motorooter.error_codes import ErrorCode as ErrorCode
from motorooter.routing.errors import (
    InvalidRequest,
    NoRouteFound,
    ProviderNotFound,
    ProviderUnavailable,
    QuotaExceeded,
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
    ProviderUnavailable: (502, ErrorCode.PROVIDER_UNAVAILABLE),
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
