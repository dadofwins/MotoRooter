"""Name to verified place, shared by discovery and by waypoints.

Extracted rather than reimplemented. Discovery has done this since the resolve stage existed —
`textQuery` with a location bias, returning a `place_id` and a coordinate — and a waypoint
search is the same operation asked by a different caller. A second implementation would agree
with the first today and drift by next week, and both would keep answering plausibly.

The difference is how many answers each wants. Discovery resolves one claim to one place or
drops it; a rider typing "Leavenworth" needs to be shown that there are three.
"""

import httpx
import pytest
import respx

from motorooter.planning.discovery.errors import DiscoveryRateLimited, DiscoveryUnavailable
from motorooter.planning.discovery.lookup import PlaceLookup
from motorooter.planning.discovery.resolve import PLACES_SEARCH_URL
from motorooter.routing.models import Coordinate

NEAR = Coordinate(lat=47.5, lon=-120.5)


def place(name: str, *, place_id: str = "ChIJ_x", lat: float = 47.59, lon: float = -120.66):
    return {
        "id": place_id,
        "displayName": {"text": name},
        "location": {"latitude": lat, "longitude": lon},
        "types": ["locality", "political"],
    }


def lookup(**overrides):
    return PlaceLookup(**({"api_key": "places-test-key"} | overrides))


class TestFindingPlacesByName:
    async def test_it_returns_what_places_found(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"places": [place("Leavenworth")]})
            )
            found = await lookup().search("Leavenworth")
        assert [item.name for item in found] == ["Leavenworth"]

    async def test_it_returns_several(self):
        """The whole reason this is not `resolve`: ambiguity is the answer, not a failure."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "places": [
                            place("Leavenworth", place_id="ChIJ_wa"),
                            place("Leavenworth", place_id="ChIJ_ks", lat=39.31, lon=-94.92),
                        ]
                    },
                )
            )
            found = await lookup().search("Leavenworth")
        assert len(found) == 2
        assert {item.place_id for item in found} == {"ChIJ_wa", "ChIJ_ks"}

    async def test_nothing_found_is_empty_not_an_error(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(200, json={}))
            assert await lookup().search("asdfghjkl") == ()

    async def test_it_carries_the_place_id(self):
        """The only field Google's terms let us keep, and how a caller refers to the place
        later without resolving it again."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"places": [place("X", place_id="ChIJ_q")]})
            )
            assert (await lookup().search("X"))[0].place_id == "ChIJ_q"

    async def test_it_carries_the_types(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"places": [place("X")]})
            )
            assert "locality" in (await lookup().search("X"))[0].kinds


class TestTheLocationBias:
    async def test_a_near_point_is_sent_as_a_bias(self):
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"places": [place("X")]})
            )
            await lookup().search("Leavenworth", near=NEAR)
        import json

        sent = json.loads(route.calls.last.request.content)
        centre = sent["locationBias"]["circle"]["center"]
        assert centre["latitude"] == pytest.approx(NEAR.lat)

    async def test_no_near_point_sends_no_bias(self):
        """An empty trip has nothing to bias from, and inventing a centre would silently
        prefer one of several real answers."""
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"places": [place("X")]})
            )
            await lookup().search("Leavenworth")
        import json

        assert "locationBias" not in json.loads(route.calls.last.request.content)

    async def test_it_asks_for_more_than_one(self):
        """`resolve` asks for one because it wants a fact. This wants the choice."""
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"places": [place("X")]})
            )
            await lookup().search("X")
        import json

        assert json.loads(route.calls.last.request.content)["maxResultCount"] > 1


class TestFailuresUseTheSharedHierarchy:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [(429, DiscoveryRateLimited), (503, DiscoveryUnavailable)],
    )
    async def test_upstream_failures_are_translated(self, status, expected):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(status, json={}))
            with pytest.raises(expected):
                await lookup().search("X")

    async def test_a_malformed_entry_is_skipped_not_fatal(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(
                    200, json={"places": ["a string", {"id": "no-location"}, place("Good")]}
                )
            )
            found = await lookup().search("X")
        assert [item.name for item in found] == ["Good"]
