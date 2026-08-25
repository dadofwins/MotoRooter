"""Domain exceptions to HTTP responses.

Registered centrally so no router needs try/except around storage or routing calls, and so
every error body has the same shape: `{code, detail}`, with `detail` always a string.

Status and code both come from `error_codes.ERROR_TABLE` — this module chooses neither.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from motorooter.api.error_codes import ErrorCode, resolve
from motorooter.api.errors import NotImplementedYet
from motorooter.routing.errors import RoutingError
from motorooter.trips.errors import TripError
from motorooter.trips.slug import InvalidSlug


def _body(code: ErrorCode, exc: Exception) -> dict[str, str]:
    """The one error body shape. `detail` is always a string, never a list."""
    return {"code": code.value, "detail": str(exc)}


def _mapped(exc: Exception) -> JSONResponse:
    status, code = resolve(exc)
    return JSONResponse(status_code=status, content=_body(code, exc))


def register_exception_handlers(app: FastAPI) -> None:
    # One handler per base class; the status and code come from the table, so adding an
    # exception type never means touching this function.
    for base in (RoutingError, TripError, InvalidSlug, NotImplementedYet):

        @app.exception_handler(base)
        async def _handle(_: Request, exc: Exception) -> JSONResponse:
            return _mapped(exc)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_: Request, exc: Exception) -> JSONResponse:
        """Malformed request bodies and path/query parameters.

        FastAPI's built-in handler answers `{"detail": [ ... ]}` — no `code`, and `detail`
        as a list of error dicts rather than a string. That contradicts the `ErrorResponse`
        shape the OpenAPI document declares for 422, so a client reading `body.code` gets
        `undefined`. Overriding it makes the declared contract true.

        This also covers domain model validators (leg contiguity, waypoint indices,
        unverified POIs pinned to the route): those run while the body is parsed into
        `UpdateTripRequest`, so pydantic's `ValidationError` is wrapped in a
        `RequestValidationError` before a handler of its own could see it.
        """
        return JSONResponse(status_code=422, content=_body(ErrorCode.VALIDATION_ERROR, exc))

    @app.exception_handler(ValidationError)
    async def _model_validation(_: Request, exc: Exception) -> JSONResponse:
        """Model validation raised outside request parsing.

        Rare — most invalid input is caught above — but a validator tripped while building a
        response or re-validating a stored object would otherwise surface as a 500.
        """
        return JSONResponse(status_code=422, content=_body(ErrorCode.VALIDATION_ERROR, exc))
