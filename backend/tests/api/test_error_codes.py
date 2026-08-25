"""Error codes are a wire contract, not an implementation detail.

These tests exist because codes used to be derived from exception class names, which made a
Python rename a silent breaking change for every client. The drift tests below are the point
of the module: they fail when the table, the enum, and the exceptions stop agreeing.
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from motorooter.api.error_codes import ERROR_TABLE, STARTUP_ONLY, ErrorCode, resolve
from motorooter.app import create_app
from motorooter.routing import errors as routing_errors
from motorooter.routing.errors import NoRouteFound, ProviderUnavailable, RoutingError
from motorooter.routing.factory import RoutingSettings
from motorooter.trips import errors as trip_errors
from motorooter.trips.errors import TripError, TripNotFound


@pytest.fixture
def client():
    return TestClient(create_app(RoutingSettings(offline=True)))


def _concrete_subclasses(base: type[Exception]) -> set[type[Exception]]:
    module = routing_errors if base is RoutingError else trip_errors
    return {
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, base) and obj is not base
    }


class TestNoDrift:
    @pytest.mark.parametrize("base", [RoutingError, TripError])
    def test_every_domain_exception_is_mapped(self, base):
        """A new exception with no entry would answer 500 with `internal_error`."""
        unmapped = _concrete_subclasses(base) - set(ERROR_TABLE) - STARTUP_ONLY
        assert not unmapped, f"add these to ERROR_TABLE: {sorted(c.__name__ for c in unmapped)}"

    def test_every_table_code_is_a_declared_enum_member(self):
        for _, code in ERROR_TABLE.values():
            assert isinstance(code, ErrorCode)

    def test_codes_are_unique_per_exception(self):
        codes = [code for _, code in ERROR_TABLE.values()]
        assert len(codes) == len(set(codes)), "two exceptions share a wire code"

    def test_startup_only_exceptions_are_not_mapped(self):
        """Mapping one would imply it can reach a request handler. It cannot."""
        assert STARTUP_ONLY.isdisjoint(ERROR_TABLE)


class TestResolve:
    def test_maps_a_known_exception(self):
        assert resolve(TripNotFound("x")) == (404, ErrorCode.TRIP_NOT_FOUND)

    def test_walks_the_mro_for_an_unlisted_subclass(self):
        """A subclass added later inherits its parent rather than falling through to 500."""

        class TransientOutage(ProviderUnavailable):
            pass

        assert resolve(TransientOutage("down")) == (502, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_unmapped_exception_is_an_internal_error(self):
        assert resolve(RuntimeError("boom")) == (500, ErrorCode.INTERNAL_ERROR)


class TestOpenApiExposure:
    """The frontend generates its union from this instead of restating it."""

    @pytest.fixture
    def schema(self, client):
        return client.get("/openapi.json").json()

    def test_error_code_is_published_as_an_enum(self, schema):
        assert "ErrorCode" in schema["components"]["schemas"]

    def test_enum_lists_every_declared_code(self, schema):
        published = set(schema["components"]["schemas"]["ErrorCode"]["enum"])
        assert published == {code.value for code in ErrorCode}

    def test_error_response_references_the_enum(self, schema):
        code_field = schema["components"]["schemas"]["ErrorResponse"]["properties"]["code"]
        assert "ErrorCode" in str(code_field)


class TestOverTheWire:
    """Codes the client actually receives must be enum members, not free strings."""

    @pytest.mark.parametrize(
        ("call", "expected"),
        [
            (lambda c: c.get("/api/trips/no-such-trip"), ErrorCode.TRIP_NOT_FOUND),
            (lambda c: c.get("/api/trips/UPPER"), ErrorCode.INVALID_SLUG),
            (
                lambda c: c.post("/api/routing/leg", json={"waypoints": [], "intent": "unpaved"}),
                ErrorCode.VALIDATION_ERROR,
            ),
            (lambda c: c.get("/api/places/ChIJ_x"), ErrorCode.NOT_IMPLEMENTED),
        ],
    )
    def test_response_code_is_a_declared_member(self, client, call, expected):
        assert call(client).json()["code"] == expected.value


def test_renaming_an_exception_no_longer_changes_the_wire_code():
    """The regression this module exists to prevent.

    The old implementation derived the code from the class name, so a rename silently
    changed the contract. Codes are now literals in the table, independent of the name.
    """
    status, code = ERROR_TABLE[NoRouteFound]
    assert code is ErrorCode.NO_ROUTE_FOUND
    assert code.value == "no_route_found"
    assert status == 422
