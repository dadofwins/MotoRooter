"""Domain exceptions to HTTP responses.

Registered centrally so no router needs try/except around storage or routing calls, and so
every error body has the same shape. `code` is the stable identifier the frontend switches
on; `detail` is the human-readable message.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

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


def _json(status: int, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": _code(exc), "detail": str(exc)})


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

    @app.exception_handler(ValidationError)
    async def _model_validation(_: Request, exc: Exception) -> JSONResponse:
        """Domain invariants violated by an otherwise well-formed request body.

        FastAPI only converts validation errors raised while *parsing* a request. Model
        validators that run later — leg contiguity, waypoint indices, unverified POIs
        pinned to the route — raise here instead, and without this handler they would
        surface as a 500 rather than the client error they are.
        """
        return JSONResponse(
            status_code=422,
            content={"code": "validation_error", "detail": str(exc)},
        )
