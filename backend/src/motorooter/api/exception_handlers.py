"""Domain exceptions to HTTP responses.

Registered centrally so no router needs try/except around storage or routing calls, and so
every error body has the same shape. `code` is the stable identifier the frontend switches
on; `detail` is the human-readable message.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from motorooter.api.errors import NotImplementedYet
from motorooter.routing.errors import (
    InvalidRequest,
    NoRouteFound,
    ProviderNotFound,
    ProviderUnavailable,
    QuotaExceeded,
    RoutingError,
    UnsupportedIntent,
)
from motorooter.trips.errors import (
    TripAlreadyExists,
    TripError,
    TripNotFound,
    TripStorageUnavailable,
)
from motorooter.trips.slug import InvalidSlug

_ROUTING_STATUS: dict[type[Exception], int] = {
    InvalidRequest: 400,
    UnsupportedIntent: 400,
    ProviderNotFound: 404,
    NoRouteFound: 422,
    QuotaExceeded: 429,
    ProviderUnavailable: 502,
}

_TRIP_STATUS: dict[type[Exception], int] = {
    TripNotFound: 404,
    TripAlreadyExists: 409,
    TripStorageUnavailable: 503,
}


def _code(exc: Exception) -> str:
    """snake_case identifier derived from the class name, e.g. `no_route_found`."""
    name = type(exc).__name__
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


def _envelope(code: str, exc: Exception) -> dict[str, str]:
    """The one error body shape. `detail` is always a string, never a list."""
    return {"code": code, "detail": str(exc)}


def _json(status: int, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=status, content=_envelope(_code(exc), exc))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RoutingError)
    async def _routing(_: Request, exc: Exception) -> JSONResponse:
        # 500 rather than a guess: an unmapped RoutingError is a gap in this table, and
        # should look like a server bug instead of being silently blamed on the client.
        return _json(_ROUTING_STATUS.get(type(exc), 500), exc)

    @app.exception_handler(TripError)
    async def _trip(_: Request, exc: Exception) -> JSONResponse:
        return _json(_TRIP_STATUS.get(type(exc), 500), exc)

    @app.exception_handler(InvalidSlug)
    async def _slug(_: Request, exc: Exception) -> JSONResponse:
        return _json(400, exc)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_: Request, exc: Exception) -> JSONResponse:
        """Malformed request bodies and path/query parameters.

        FastAPI's built-in handler answers with `{"detail": [ ... ]}` — no `code`, and
        `detail` as a list of error dicts rather than a string. That contradicts the
        `ErrorResponse` shape the OpenAPI document declares for 422, so clients reading
        `body.code` get `undefined`. Overriding it makes the declared contract true.

        This also covers domain model validators (leg contiguity, waypoint indices,
        unverified POIs pinned to the route): those run while the body is being parsed into
        `UpdateTripRequest`, so pydantic's `ValidationError` is wrapped in a
        `RequestValidationError` before it can reach a handler of its own.
        """
        return JSONResponse(status_code=422, content=_envelope("validation_error", exc))

    @app.exception_handler(ValidationError)
    async def _model_validation(_: Request, exc: Exception) -> JSONResponse:
        """Model validation raised outside request parsing.

        Rare — most invalid input is caught above — but a validator tripped while building a
        response or re-validating a stored object would otherwise surface as a 500.
        """
        return JSONResponse(status_code=422, content=_envelope("validation_error", exc))

    @app.exception_handler(NotImplementedYet)
    async def _not_implemented(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=501, content=_envelope("not_implemented", exc))
