"""`RouteIncomplete` at the HTTP boundary.

`test_error_codes.py` already forces every domain exception into `ERROR_TABLE`, so this
covers only what that cannot: the status choice is deliberate rather than incidental, and the
message carries enough for a client to act on.
"""

from fastapi.testclient import TestClient

from motorooter.api.error_codes import ErrorCode, resolve
from motorooter.app import create_app
from motorooter.routing.errors import RouteIncomplete
from motorooter.routing.factory import RoutingSettings


def test_route_incomplete_is_client_actionable_rather_than_a_server_fault():
    """The trip exists and the request was well formed; the fix is to press Replan."""
    assert resolve(RouteIncomplete((1,))) == (422, ErrorCode.ROUTE_INCOMPLETE)


def test_it_names_the_offending_legs():
    """A client has to know which section to fix, not merely that something is missing."""
    message = str(RouteIncomplete((2, 5)))
    assert "2" in message
    assert "5" in message


def test_it_travels_through_the_standard_error_envelope():
    app = create_app(RoutingSettings(offline=True))

    @app.get("/api/test-route-incomplete")
    async def _boom() -> None:
        raise RouteIncomplete((1,))

    response = TestClient(app, raise_server_exceptions=False).get("/api/test-route-incomplete")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.ROUTE_INCOMPLETE.value
    assert isinstance(response.json()["detail"], str)
