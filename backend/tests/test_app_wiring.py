"""Does `create_app` actually attach the services the endpoints look for?

The sixth thing on this project to be merged correct, tested, and called by nobody was
`PlaceDetails`: fully implemented, fully tested, and constructed nowhere outside tests. The
endpoint read `app.state.places`, found nothing, and answered 501 forever.

Every existing Places test assigned `app.state.places` itself, which is precisely why the gap
was invisible — a test that wires its own dependency cannot notice that production never
does. So these tests build the app the way the deployment does and assert nothing is missing,
which is a different question from whether any individual service works.
"""

import pytest
from fastapi.testclient import TestClient

from motorooter.api.deps import OPTIONAL_SERVICES
from motorooter.app import create_app
from motorooter.routing.factory import RoutingSettings

LIVE_KEYS = {
    "BRAVE_SEARCH_API_KEY": "brave-test",
    "OPENAI_API_KEY": "sk-test",
    "GOOGLE_MAPS_SERVER_KEY": "places-test",
}


@pytest.fixture
def configured(monkeypatch):
    for name, value in LIVE_KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MOTOROOTER_OFFLINE", raising=False)
    monkeypatch.setenv("MOTOROOTER_TRIPS_EPHEMERAL", "1")
    # Real-looking keys, no live calls: the question here is whether the app assembles what
    # the endpoints look for, not whether any of it can reach an API.
    return create_app(RoutingSettings(ors_api_key="ors-test", google_api_key="google-test"))


class TestEveryOptionalServiceIsWired:
    @pytest.mark.parametrize("name", sorted(OPTIONAL_SERVICES))
    def test_the_attribute_exists(self, name, configured):
        """Absent is different from None. A missing attribute means nobody assigned it."""
        assert hasattr(configured.state, name)

    @pytest.mark.parametrize("name", sorted(OPTIONAL_SERVICES))
    def test_it_is_built_when_the_credentials_are_there(self, name, configured):
        """The bug: credentials present, service still None, endpoint still 501."""
        assert getattr(configured.state, name) is not None

    @pytest.mark.parametrize("name", sorted(OPTIONAL_SERVICES))
    def test_it_is_none_offline(self, name, monkeypatch):
        """Offline means no external services at all, not some of them."""
        for key in LIVE_KEYS:
            monkeypatch.delenv(key, raising=False)
        app = create_app(RoutingSettings(offline=True))
        assert getattr(app.state, name) is None


class TestThePlacesEndpointIsReachable:
    def test_it_does_not_answer_501_when_configured(self, configured):
        """The whole bug in one assertion. It answers 501 only when unconfigured.

        Places is mocked rather than reached: what is under test is whether the request gets
        past the dependency at all, and the first version of this test proved the wiring by
        making a real call with a fake key, which is not a test this suite may contain.
        """
        import httpx
        import respx

        from motorooter.planning.discovery.details import PLACE_DETAILS_URL

        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=PLACE_DETAILS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "ChIJ_anything",
                        "displayName": {"text": "Somewhere"},
                        "location": {"latitude": 47.0, "longitude": -121.0},
                        "types": ["campground"],
                    },
                )
            )
            response = TestClient(configured).get("/api/places/ChIJ_anything")
        assert response.status_code != 501

    def test_it_answers_501_when_there_is_no_key(self, monkeypatch):
        for key in LIVE_KEYS:
            monkeypatch.delenv(key, raising=False)
        app = create_app(RoutingSettings(offline=True))
        assert TestClient(app).get("/api/places/ChIJ_anything").status_code == 501


class TestForgettingOneIsAStartupFailure:
    """Assigning in one place is not the property that matters; refusing to boot is.

    Six things on this project have been merged correct, tested, and called by nobody, and
    the common factor every time is that nothing failed when the wiring was omitted. A
    service named in `OPTIONAL_SERVICES` and not built is now a startup error, which fails
    the deploy rather than one endpoint — the same choice `RoutingConfigError` makes for a
    policy that would route dirt through a paved-only engine.
    """

    def test_a_declared_but_unbuilt_service_refuses_to_start(self, monkeypatch):
        monkeypatch.setattr(
            "motorooter.api.services.OPTIONAL_SERVICES",
            (*OPTIONAL_SERVICES, "a_service_nobody_built"),
        )
        with pytest.raises(RuntimeError, match="a_service_nobody_built"):
            create_app(RoutingSettings(offline=True))

    def test_the_error_names_what_is_missing(self, monkeypatch):
        """So the fix is obvious from the message rather than from reading two modules."""
        monkeypatch.setattr(
            "motorooter.api.services.OPTIONAL_SERVICES", (*OPTIONAL_SERVICES, "forgotten")
        )
        with pytest.raises(RuntimeError) as caught:
            create_app(RoutingSettings(offline=True))
        assert "declared but not built" in str(caught.value)
