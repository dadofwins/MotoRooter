"""The route-through-best endpoint: the mouse's half of the same capability.

It exists because of the rule the whole design rests on — every tool the assistant can call
must be reachable by mouse. "Route me through the good ones" is a capability, not a chat
feature, and a checkbox on the Replan button would have made it a chat-first one with an
affordance bolted on.

Both this and the tool are thin wrappers over `route_through_best`, so what is tested here is
the HTTP shape and the refusals, not the selection.
"""

from math import pi

from fastapi.testclient import TestClient

from motorooter.app import create_app
from motorooter.routing.factory import RoutingSettings
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import Poi, PoiCategory, PoiSource, Trip, TripLeg, Waypoint
from motorooter.trips.store import InMemoryTripStore
from tests.trips.store_contract import T0

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180
LENGTH_M = 20_000.0
URL = "/api/trips/oregon-backcountry/route-through-best"


def north(metres: float) -> Coordinate:
    return Coordinate(lat=45.0 + metres / M_PER_DEGREE_LAT, lon=-121.0)


def leg() -> RouteLeg:
    points = tuple(north(LENGTH_M * index / 40) for index in range(41))
    return RouteLeg(
        geometry=points,
        distance_m=LENGTH_M,
        duration_s=1200.0,
        provider="fake",
        intent=LegIntent.UNPAVED,
    )


def a_poi(name: str, score: float | None, along: float = 0.5) -> Poi:
    return Poi(
        id=f"poi-{name}",
        name=name,
        category=PoiCategory.CAMPGROUND,
        coordinate=north(LENGTH_M * along),
        source=PoiSource.PLACES,
        place_id=f"ChIJ-{name}",
        score=score,
        note="Well-regarded and barely off the line.",
    )


def trip(*pois: Poi, routed: bool = True) -> Trip:
    return Trip(
        slug="oregon-backcountry",
        name="Oregon Backcountry",
        created_at=T0,
        edited_at=T0,
        waypoints=(
            Waypoint(coordinate=north(0), name="start"),
            Waypoint(coordinate=north(LENGTH_M), name="end"),
        ),
        legs=(
            TripLeg(
                intent=LegIntent.UNPAVED,
                start_waypoint_index=0,
                end_waypoint_index=1,
                routed=leg() if routed else None,
            ),
        ),
        pois=pois,
    )


async def client_for(document: Trip) -> TestClient:
    store = InMemoryTripStore()
    await store.create(document)
    return TestClient(create_app(RoutingSettings(offline=True), trip_store=store))


class TestRoutingThrough:
    async def test_it_answers_ok(self):
        client = await client_for(trip(a_poi("Lion Rock", 0.95)))
        assert client.post(URL, json={}).status_code == 200

    async def test_it_returns_the_saved_trip_so_the_map_can_redraw_without_a_second_call(self):
        client = await client_for(trip(a_poi("Lion Rock", 0.95)))
        body = client.post(URL, json={}).json()
        assert [point["name"] for point in body["trip"]["waypoints"]] == [
            "start",
            "Lion Rock",
            "end",
        ]

    async def test_it_says_what_it_added_so_the_change_can_be_shown(self):
        client = await client_for(trip(a_poi("Lion Rock", 0.95)))
        body = client.post(URL, json={}).json()
        assert [place["name"] for place in body["added"]] == ["Lion Rock"]

    async def test_the_added_places_carry_the_reason_they_were_chosen(self):
        client = await client_for(trip(a_poi("Lion Rock", 0.95)))
        body = client.post(URL, json={}).json()
        assert body["added"][0]["note"] == "Well-regarded and barely off the line."

    async def test_it_says_what_it_left_out_so_a_bound_is_not_silence(self):
        found = [a_poi(f"p{index}", 0.9, along=0.2 + index / 5) for index in range(4)]
        client = await client_for(trip(*found))
        body = client.post(URL, json={"limit": 1}).json()
        assert len(body["added"]) == 1
        assert len(body["left_out"]) == 3

    async def test_a_limit_the_caller_asks_for_is_honoured(self):
        found = [a_poi(f"p{index}", 0.9, along=0.2 + index / 5) for index in range(4)]
        client = await client_for(trip(*found))
        assert len(client.post(URL, json={"limit": 3}).json()["added"]) == 3

    async def test_asking_for_none_changes_nothing(self):
        client = await client_for(trip(a_poi("Lion Rock", 0.95)))
        body = client.post(URL, json={"limit": 0}).json()
        assert body["added"] == []
        assert len(body["trip"]["waypoints"]) == 2


class TestWhenItCannot:
    async def test_an_unrouted_trip_is_a_route_incomplete_not_a_crash(self):
        """422, the same code the trip router uses, and distinguishable by `code` from the
        other 422 on this endpoint — a limit the schema rejected."""
        client = await client_for(trip(a_poi("Lion Rock", 0.95), routed=False))
        response = client.post(URL, json={})
        assert response.status_code == 422
        assert response.json()["code"] == "route_incomplete"

    async def test_calling_it_twice_without_rerouting_between_is_refused(self):
        """Documented rather than discovered, because a live run discovered it.

        Any edit that moves waypoints leaves the legs it touched without geometry — they are
        stale by definition — and the map routes them again. Until it does, there is no
        corridor to measure a detour against, so a second press in that window is refused
        rather than answered against a route that no longer exists. The client must route
        between presses; the refusal says so instead of guessing.
        """
        client = await client_for(trip(a_poi("Lion Rock", 0.95)))
        assert client.post(URL, json={}).status_code == 200
        assert client.post(URL, json={}).json()["code"] == "route_incomplete"

    async def test_an_unknown_trip_is_a_404(self):
        client = await client_for(trip())
        assert client.post("/api/trips/no-such-trip/route-through-best", json={}).status_code == 404

    async def test_a_bad_slug_is_rejected_before_it_becomes_a_storage_path(self):
        client = await client_for(trip())
        assert client.post("/api/trips/..%2Fetc/route-through-best", json={}).status_code != 200

    async def test_a_negative_limit_is_refused_by_the_schema(self):
        client = await client_for(trip(a_poi("Lion Rock", 0.95)))
        assert client.post(URL, json={"limit": -1}).status_code == 422
