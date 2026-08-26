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


@pytest.fixture
def places_returning():
    """A client whose Places answers with one scripted body.

    These tests still assign `app.state.places` themselves — which is exactly how the
    endpoint came to answer 501 in production while every test here passed. That gap is
    covered by `tests/test_app_wiring.py`, which builds the app the way the deployment does;
    these stay focused on what the endpoint does with a response it got.
    """
    with respx.mock(assert_all_called=False) as mock:

        def build(body: dict[str, Any]) -> TestClient:
            mock.get(url__startswith=PLACE_DETAILS_URL).mock(
                return_value=httpx.Response(200, json=body)
            )
            app = create_app(RoutingSettings(offline=True))
            app.state.places = PlaceDetails(api_key="places-test-key")
            return TestClient(app)

        yield build


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


class TestPhotos:
    """`photo_urls` was declared, frozen into the contract, and never populated.

    The field mask deliberately omitted photos — "until something renders them", which was
    correct when written. Something renders them now, and frontend asked exactly the right
    question rather than guessing: does the string carry a resource name, a URL needing a
    key appended, or something a browser can load?

    **A URL a browser can load, and nothing else.** The alternatives both leak the server
    key into an unauthenticated page or make the client construct Google URLs, and neither
    is a thing to hand a client that only knows how to put strings in `src`.
    """

    @staticmethod
    def _body(**extra):
        return {
            "id": "ChIJ_halfway",
            "displayName": {"text": "Halfway Flat"},
            "location": {"latitude": 46.87, "longitude": -121.52},
            "types": ["campground"],
        } | extra

    @staticmethod
    def _photos(count: int):
        return [
            {"name": f"places/ChIJ_halfway/photos/ref{index}", "widthPx": 3000}
            for index in range(count)
        ]

    def test_the_mask_asks_for_photos(self):
        from motorooter.planning.discovery.details import DETAIL_FIELD_MASK

        assert "photos" in DETAIL_FIELD_MASK

    def test_a_photo_reference_becomes_a_loadable_url(self, places_returning):
        client = places_returning(self._body(photos=self._photos(1)))
        urls = client.get("/api/places/ChIJ_halfway").json()["detail"]["photo_urls"]
        assert urls
        assert urls[0].startswith("https://")

    def test_the_url_carries_the_photo_reference(self, places_returning):
        client = places_returning(self._body(photos=self._photos(1)))
        urls = client.get("/api/places/ChIJ_halfway").json()["detail"]["photo_urls"]
        assert "places/ChIJ_halfway/photos/ref0" in urls[0]

    def test_no_photos_is_an_empty_list_not_an_error(self, places_returning):
        """Dispersed camping is what this app cares most about and what Places knows least
        about. A dialog with a name and a location is a valid answer."""
        client = places_returning(self._body())
        assert client.get("/api/places/ChIJ_halfway").json()["detail"]["photo_urls"] == []

    def test_only_a_few_are_returned(self, places_returning):
        """Each one is a second request when the browser loads it, per dialog open. Ten
        photos for a place nobody scrolls is a cost with no benefit — the same reasoning
        that kept photos out of the mask in the first place, applied to the count."""
        from motorooter.api.routers.places import MAX_PHOTOS

        client = places_returning(self._body(photos=self._photos(10)))
        urls = client.get("/api/places/ChIJ_halfway").json()["detail"]["photo_urls"]
        assert len(urls) == MAX_PHOTOS
        assert MAX_PHOTOS <= 3

    def test_a_malformed_photo_entry_is_skipped(self, places_returning):
        client = places_returning(
            self._body(photos=[{"widthPx": 100}, "a string", None, *self._photos(1)])
        )
        urls = client.get("/api/places/ChIJ_halfway").json()["detail"]["photo_urls"]
        assert len(urls) == 1


class TestThePhotoKeyIsSeparable:
    """A photo URL carries its key into an unauthenticated page.

    Today the Maps browser key and the Places *server* key are the same value, so every POI
    dialog would hand a visitor a working, unrestricted key the moment there is a public URL
    — billing exposure with no ceiling on an app that is world-readable by design.

    The fix is a second key, restricted by HTTP referrer and to Places Photos, used only for
    the URLs that reach a browser. It falls back rather than being required: a missing key
    must not 501 the dialog on a prototype nobody has deployed. But the fallback is *loud*,
    because a deployment silently publishing its server key is exactly what should be noisy.
    """

    def test_the_photo_key_is_used_when_configured(self, places_returning):
        client = places_returning(
            {
                "id": "ChIJ_x",
                "displayName": {"text": "Somewhere"},
                "location": {"latitude": 47.0, "longitude": -121.0},
                "types": ["campground"],
                "photos": [{"name": "places/ChIJ_x/photos/ref0"}],
            }
        )
        client.app.state.places = PlaceDetails(api_key="server-key", photo_key="browser-key")
        urls = client.get("/api/places/ChIJ_x").json()["detail"]["photo_urls"]
        assert "browser-key" in urls[0]
        assert "server-key" not in urls[0]

    def test_it_falls_back_to_the_server_key(self, places_returning):
        """So a prototype with one key still shows photos."""
        client = places_returning(
            {
                "id": "ChIJ_x",
                "displayName": {"text": "Somewhere"},
                "location": {"latitude": 47.0, "longitude": -121.0},
                "types": ["campground"],
                "photos": [{"name": "places/ChIJ_x/photos/ref0"}],
            }
        )
        client.app.state.places = PlaceDetails(api_key="server-key")
        urls = client.get("/api/places/ChIJ_x").json()["detail"]["photo_urls"]
        assert "server-key" in urls[0]

    def test_the_fallback_is_reported(self):
        """`photo_key_is_shared` is what the startup warning reads."""
        assert PlaceDetails(api_key="k").photo_key_is_shared is True
        assert PlaceDetails(api_key="k", photo_key="b").photo_key_is_shared is False
