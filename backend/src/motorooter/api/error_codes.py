"""The wire vocabulary of error codes, and the single mapping from exception to response.

Codes used to be derived from exception class names. That made a Python refactor a silent
breaking change to the HTTP contract: renaming `NoRouteFound` would have changed the wire
code from `no_route_found` to something else with no test, no type error, and no build
failure anywhere — least of all in the frontend, which was hand-maintaining the matching
union because the schema typed `code` as a bare string.

So codes are declared here as literals, the table below is the only place a status is
chosen, and `ErrorCode` is exported into the OpenAPI document as an enum. The frontend
generates its union from that instead of restating it.

Adding an error means adding it here. `test_error_codes.py` fails if an exception the API
can raise is missing from the table, so the two cannot drift apart.
"""

from enum import StrEnum

from motorooter.api.errors import NotImplementedYet
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


class ErrorCode(StrEnum):
    """Stable, machine-readable error identifiers.

    Clients switch on these. `detail` is for humans and is not part of the contract.
    Renaming a member is a breaking change; adding one is not.
    """

    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_INTENT = "unsupported_intent"
    PROVIDER_NOT_FOUND = "provider_not_found"
    NO_ROUTE_FOUND = "no_route_found"
    ROUTE_INCOMPLETE = "route_incomplete"
    QUOTA_EXCEEDED = "quota_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"

    INVALID_SLUG = "invalid_slug"
    TRIP_NOT_FOUND = "trip_not_found"
    TRIP_ALREADY_EXISTS = "trip_already_exists"
    TRIP_MODIFIED_CONCURRENTLY = "trip_modified_concurrently"
    TRIP_DOCUMENT_INVALID = "trip_document_invalid"
    TRIP_STORAGE_UNAVAILABLE = "trip_storage_unavailable"

    VALIDATION_ERROR = "validation_error"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_ERROR = "internal_error"


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
