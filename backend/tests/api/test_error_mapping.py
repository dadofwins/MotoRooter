"""Domain errors introduced by the planning layer, mapped to HTTP.

An unmapped `RoutingError` falls through to 500 by design, which is right for a wiring bug
and wrong for a state the client can act on. `RouteIncomplete` is the latter: asking to
export or stitch a trip whose legs are not all routed is a 422, and the client's next move
is to press Replan.
"""

from fastapi.testclient import TestClient

from motorooter.api.exception_handlers import _ROUTING_STATUS
from motorooter.app import create_app
from motorooter.routing.errors import RouteIncomplete
from motorooter.routing.factory import RoutingSettings


def test_route_incomplete_is_a_client_actionable_422():
    assert _ROUTING_STATUS[RouteIncomplete] == 422


def test_route_incomplete_names_the_offending_legs():
    """The client has to know which section to fix, not merely that something is missing."""
    assert "2" in str(RouteIncomplete((2, 5)))
    assert "5" in str(RouteIncomplete((2, 5)))


def test_it_travels_through_the_standard_error_envelope():
    """Every error body carries a `code` the frontend can switch on."""
    app = create_app(RoutingSettings(offline=True))

    @app.get("/api/test-route-incomplete")
    async def _boom() -> None:
        raise RouteIncomplete((1,))

    response = TestClient(app, raise_server_exceptions=False).get("/api/test-route-incomplete")
    assert response.status_code == 422
    assert response.json()["code"] == "route_incomplete"
    assert isinstance(response.json()["detail"], str)
