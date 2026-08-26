"""`GET /api/geocode`.

The mouse half of the both-paths rule for place-name entry, and it closes something older than
today: Tim's original trip-creation spec was "type a starting and ending address or choose to
click on the map". The clicking shipped; the typing did not, because geocoding never existed.

Several results, deliberately. A name is a claim until something verifies it, and plenty of
names verify to more than one real place.
"""

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from motorooter.app import create_app
from motorooter.planning.discovery.lookup import PLACES_SEARCH_URL, PlaceLookup
from motorooter.routing.factory import RoutingSettings


def place(name: str, place_id: str = "ChIJ_x", lat: float = 47.59, lon: float = -120.66):
    return {
        "id": place_id,
        "displayName": {"text": name},
        "location": {"latitude": lat, "longitude": lon},
        "types": ["locality", "political"],
    }


@pytest.fixture
def client_returning():
    with respx.mock(assert_all_called=False) as mock:

        def build(*places: dict[str, Any]) -> TestClient:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"places": list(places)})
            )
            app = create_app(RoutingSettings(offline=True))
            app.state.place_lookup = PlaceLookup(api_key="places-test-key")
            client = TestClient(app)
            client.places_mock = mock
            return client

        yield build


class TestSearchingByName:
    def test_it_answers_ok(self, client_returning):
        client = client_returning(place("Leavenworth"))
        assert client.get("/api/geocode", params={"q": "Leavenworth"}).status_code == 200

    def test_it_returns_the_matches(self, client_returning):
        client = client_returning(place("Leavenworth"))
        body = client.get("/api/geocode", params={"q": "Leavenworth"}).json()
        assert body["results"][0]["name"] == "Leavenworth"
        assert body["results"][0]["coordinate"]["lat"] == pytest.approx(47.59)

    def test_it_returns_several(self, client_returning):
        client = client_returning(
            place("Leavenworth", "ChIJ_wa"), place("Leavenworth", "ChIJ_ks", 39.31, -94.92)
        )
        assert len(client.get("/api/geocode", params={"q": "Leavenworth"}).json()["results"]) == 2

    def test_the_place_id_comes_through(self, client_returning):
        """The one field a client may store."""
        client = client_returning(place("X", "ChIJ_keep"))
        body = client.get("/api/geocode", params={"q": "X"}).json()
        assert body["results"][0]["place_id"] == "ChIJ_keep"

    def test_the_kinds_come_through(self, client_returning):
        client = client_returning(place("X"))
        body = client.get("/api/geocode", params={"q": "X"}).json()
        assert "locality" in body["results"][0]["kinds"]

    def test_nothing_found_is_an_empty_list_not_an_error(self, client_returning):
        """A typo matches nothing. That is an answer."""
        client = client_returning()
        response = client.get("/api/geocode", params={"q": "asdfghjkl"})
        assert response.status_code == 200
        assert response.json()["results"] == []


class TestTheNearBias:
    def test_a_near_point_is_forwarded(self, client_returning):
        import json

        client = client_returning(place("Leavenworth"))
        client.get("/api/geocode", params={"q": "Leavenworth", "near": "47.5,-120.5"})
        sent = json.loads(client.places_mock.calls.last.request.content)
        centre = sent["locationBias"]["circle"]["center"]
        assert centre["latitude"] == pytest.approx(47.5)

    def test_no_near_point_sends_no_bias(self, client_returning):
        """An empty trip has nothing to bias from; a made-up centre would silently prefer one
        of several real places."""
        import json

        client = client_returning(place("Leavenworth"))
        client.get("/api/geocode", params={"q": "Leavenworth"})
        assert "locationBias" not in json.loads(client.places_mock.calls.last.request.content)

    def test_a_malformed_near_is_rejected(self, client_returning):
        """Better a 422 than silently ignoring the disambiguator and answering confidently."""
        client = client_returning(place("X"))
        assert client.get("/api/geocode", params={"q": "X", "near": "nonsense"}).status_code == 422

    def test_an_out_of_range_near_is_rejected(self, client_returning):
        client = client_returning(place("X"))
        response = client.get("/api/geocode", params={"q": "X", "near": "91.0,-120.5"})
        assert response.status_code == 422


class TestWhenItCannotAnswer:
    def test_an_empty_query_is_rejected(self, client_returning):
        client = client_returning(place("X"))
        assert client.get("/api/geocode", params={"q": "   "}).status_code == 422

    def test_no_places_key_is_501(self):
        """Same choice as the POI dialog: one feature disabled, not a dead backend."""
        app = create_app(RoutingSettings(offline=True))
        assert TestClient(app).get("/api/geocode", params={"q": "X"}).status_code == 501
