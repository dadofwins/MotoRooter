"""The POI dialog's data source.

Two properties this endpoint exists to hold, and both are contractual rather than incidental.

**Nothing is cached.** Google's terms permit storing `place_id` indefinitely and very little
else, so a server-side cache would breach them — and the frontend asserts the behaviour by
reopening the dialog and expecting a second request, so a cache would break their test too.

**Absence is a normal response.** Dispersed camping is what this app cares most about and
what Places knows least about. A dialog with a name and a location, no rating and no hours,
is a correct answer rather than a degraded one.
"""

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from motorooter.app import create_app
from motorooter.planning.discovery.details import PLACE_DETAILS_URL, PlaceDetails
from motorooter.routing.factory import RoutingSettings

PLACE_ID = "ChIJ_halfway"


def detail(**overrides: Any) -> dict[str, Any]:
    return {
        "id": PLACE_ID,
        "displayName": {"text": "Halfway Flat Campground"},
        "location": {"latitude": 46.872, "longitude": -121.519},
        "types": ["campground", "point_of_interest"],
    } | overrides


@pytest.fixture
def mock_places():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=PLACE_DETAILS_URL).mock(
            return_value=httpx.Response(200, json=detail())
        )
        yield mock


@pytest.fixture
def client(mock_places):
    app = create_app(RoutingSettings(offline=True))
    app.state.places = PlaceDetails(api_key="places-test-key")
    return TestClient(app)


class TestTheDialogGetsItsData:
    def test_it_answers_ok(self, client):
        assert client.get(f"/api/places/{PLACE_ID}").status_code == 200

    def test_the_name_comes_back(self, client):
        body = client.get(f"/api/places/{PLACE_ID}").json()
        assert body["detail"]["poi"]["name"] == "Halfway Flat Campground"

    def test_the_category_comes_from_places_types(self, client):
        body = client.get(f"/api/places/{PLACE_ID}").json()
        assert body["detail"]["poi"]["category"] == "campground"

    def test_the_poi_is_verified(self, client):
        """It came from Places, so it may be pinned."""
        body = client.get(f"/api/places/{PLACE_ID}").json()
        assert body["detail"]["poi"]["place_id"] == PLACE_ID

    def test_display_fields_come_through(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=PLACE_DETAILS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=detail(
                        rating=4.4,
                        userRatingCount=15,
                        websiteUri="https://example.test",
                        nationalPhoneNumber="(509) 555-0100",
                        regularOpeningHours={"weekdayDescriptions": ["Monday: Open 24 hours"]},
                        reviews=[{"text": {"text": "Quiet, good for a tent."}}],
                    ),
                )
            )
            app = create_app(RoutingSettings(offline=True))
            app.state.places = PlaceDetails(api_key="k")
            body = TestClient(app).get(f"/api/places/{PLACE_ID}").json()["detail"]

        assert body["rating"] == 4.4
        assert body["website"] == "https://example.test"
        assert body["opening_hours"] == ["Monday: Open 24 hours"]
        assert body["reviews"] == ["Quiet, good for a tent."]


class TestAbsenceIsNormal:
    def test_a_place_with_no_rating_is_still_a_success(self, client):
        """The dispersed-camping case, which is the one that matters most here."""
        response = client.get(f"/api/places/{PLACE_ID}")
        assert response.status_code == 200
        assert response.json()["detail"]["rating"] is None

    def test_no_hours_no_phone_no_reviews_is_fine(self, client):
        body = client.get(f"/api/places/{PLACE_ID}").json()["detail"]
        assert body["opening_hours"] == []
        assert body["phone"] is None
        assert body["reviews"] == []

    @pytest.mark.parametrize("bogus", [7.0, -1.0, "great", True])
    def test_an_impossible_rating_is_dropped_rather_than_shown(self, bogus):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=PLACE_DETAILS_URL).mock(
                return_value=httpx.Response(200, json=detail(rating=bogus))
            )
            app = create_app(RoutingSettings(offline=True))
            app.state.places = PlaceDetails(api_key="k")
            body = TestClient(app).get(f"/api/places/{PLACE_ID}").json()
        assert body["detail"]["rating"] is None


class TestNothingIsCached:
    def test_each_request_hits_places(self, client, mock_places):
        """The frontend reopens the dialog and expects a second request. A cache would
        break that test and Google's terms in one go."""
        client.get(f"/api/places/{PLACE_ID}")
        client.get(f"/api/places/{PLACE_ID}")
        assert mock_places.calls.call_count == 2

    def test_no_cache_headers_invite_one_downstream(self, client):
        response = client.get(f"/api/places/{PLACE_ID}")
        assert "max-age" not in response.headers.get("cache-control", "")


class TestTheCategoryFallback:
    def test_places_types_win_over_the_query_parameter(self, client):
        """Inheriting a category from the caller is how a ski resort became a wild camp."""
        body = client.get(f"/api/places/{PLACE_ID}?category=hotel").json()
        assert body["detail"]["poi"]["category"] == "campground"

    def test_the_parameter_is_used_when_places_cannot_say(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=PLACE_DETAILS_URL).mock(
                return_value=httpx.Response(200, json=detail(types=["point_of_interest"]))
            )
            app = create_app(RoutingSettings(offline=True))
            app.state.places = PlaceDetails(api_key="k")
            body = TestClient(app).get(f"/api/places/{PLACE_ID}?category=wild_camp").json()
        assert body["detail"]["poi"]["category"] == "wild_camp"

    def test_neither_is_an_error_rather_than_a_guess(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=PLACE_DETAILS_URL).mock(
                return_value=httpx.Response(200, json=detail(types=["point_of_interest"]))
            )
            app = create_app(RoutingSettings(offline=True))
            app.state.places = PlaceDetails(api_key="k")
            response = TestClient(app, raise_server_exceptions=False).get(f"/api/places/{PLACE_ID}")
        assert response.status_code >= 400


class TestFailure:
    def test_no_credentials_is_a_501(self):
        app = create_app(RoutingSettings(offline=True))
        app.state.places = None
        response = TestClient(app).get(f"/api/places/{PLACE_ID}")
        assert response.status_code == 501
        assert response.json()["code"] == "not_implemented"

    def test_an_unknown_place_is_reported(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=PLACE_DETAILS_URL).mock(return_value=httpx.Response(404))
            app = create_app(RoutingSettings(offline=True))
            app.state.places = PlaceDetails(api_key="k")
            response = TestClient(app, raise_server_exceptions=False).get(f"/api/places/{PLACE_ID}")
        assert response.status_code >= 400

    def test_a_place_with_no_location_is_refused(self):
        """Nothing without a coordinate can go on a map or be pinned to a route."""
        with respx.mock(assert_all_called=False) as mock:
            payload = detail()
            del payload["location"]
            mock.get(url__startswith=PLACE_DETAILS_URL).mock(
                return_value=httpx.Response(200, json=payload)
            )
            app = create_app(RoutingSettings(offline=True))
            app.state.places = PlaceDetails(api_key="k")
            response = TestClient(app, raise_server_exceptions=False).get(f"/api/places/{PLACE_ID}")
        assert response.status_code >= 400
