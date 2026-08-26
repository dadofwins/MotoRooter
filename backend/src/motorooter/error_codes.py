"""The wire vocabulary of error codes.

Deliberately dependency-free and at the package root. `api/error_codes.py` imports
`trips.errors` to build its table, so a domain model reaching back into the API layer for a
code would invert the dependency and sit one import away from a cycle. Everything can depend
on this; it depends on nothing.

Codes are declared as literals rather than derived from exception class names. Deriving them
made a Python rename a silent breaking change to the HTTP contract — no test, no type error,
no build failure anywhere, least of all in the frontend. `ErrorCode` is exported into the
OpenAPI document as an enum, so the frontend generates its union from here instead of
restating it.

Renaming a member is a breaking change. Adding one is not.
"""

from enum import StrEnum


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
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"

    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_QUOTA_EXCEEDED = "llm_quota_exceeded"
    LLM_REFUSED = "llm_refused"
    TOOL_CALL_FAILED = "tool_call_failed"

    INVALID_SLUG = "invalid_slug"
    TRIP_NOT_FOUND = "trip_not_found"
    TRIP_ALREADY_EXISTS = "trip_already_exists"
    TRIP_MODIFIED_CONCURRENTLY = "trip_modified_concurrently"
    TRIP_DOCUMENT_INVALID = "trip_document_invalid"
    TRIP_STORAGE_UNAVAILABLE = "trip_storage_unavailable"

    # Discovery
    DISCOVERY_UNAVAILABLE = "discovery_unavailable"
    DISCOVERY_RATE_LIMITED = "discovery_rate_limited"
    DISCOVERY_QUOTA_EXCEEDED = "discovery_quota_exceeded"
    DISCOVERY_REFUSED = "discovery_refused"
    PLACE_NOT_DISPLAYABLE = "place_not_displayable"

    VALIDATION_ERROR = "validation_error"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_ERROR = "internal_error"
