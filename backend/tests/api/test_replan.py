"""The replan endpoint: discovery behind a URL.

Two complete halves existed for most of a day — a discovery pipeline that worked only in a
spike, and POI pins with no data source. This is the bridge, and its contract is the framing
the frontend already parses: one `ReplanEvent` per line, newline-delimited, POIs accumulating
as they resolve.

The awkward property of a stream is that the status code is committed on the first byte. Any
failure after that can only be reported inside the body, so everything checkable is checked
before the stream opens.
"""

import json
from math import pi
from typing import Any

from fastapi.testclient import TestClient

from motorooter.app import create_app
from motorooter.planning.discovery.pipeline import DiscoveryProgress
from motorooter.routing.factory import RoutingSettings
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import Poi, PoiCategory, PoiSource, Trip, TripLeg, Waypoint
from motorooter.trips.store import InMemoryTripStore
from tests.trips.store_contract import T0

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180


def leg(distance_m: float = 20_000.0) -> RouteLeg:
    return RouteLeg(
        geometry=tuple(
            Coordinate(lat=index * 500.0 / M_PER_DEGREE_LAT, lon=-121.0) for index in range(40)
        ),
        distance_m=distance_m,
        duration_s=1200.0,
        provider="fake",
        intent=LegIntent.UNPAVED,
    )


def routed_trip(*legs: RouteLeg) -> Trip:
    used = legs or (leg(),)
    return Trip(
        slug="oregon-backcountry",
        name="Oregon Backcountry",
        created_at=T0,
        edited_at=T0,
        waypoints=tuple(
            Waypoint(coordinate=Coordinate(lat=45.0 + index, lon=-121.0))
            for index in range(len(used) + 1)
        ),
        legs=tuple(
            TripLeg(
                intent=LegIntent.UNPAVED,
                start_waypoint_index=index,
                end_waypoint_index=index + 1,
                routed=each,
            )
            for index, each in enumerate(used)
        ),
    )


def a_poi(name: str = "Halfway Flat") -> Poi:
    return Poi(
        id="poi-1",
        name=name,
        category=PoiCategory.CAMPGROUND,
        coordinate=Coordinate(lat=46.0, lon=-121.0),
        source=PoiSource.PLACES,
        place_id="ChIJ_x",
    )


class StubPipeline:
    """Yields a scripted sequence, recording what it was asked to search."""

    def __init__(self, *steps: DiscoveryProgress, error: Exception | None = None) -> None:
        self._steps = steps or (
            DiscoveryProgress(stage="discovery", message="searching", progress=0.5),
            DiscoveryProgress(
                stage="done", message="1 worth showing", progress=1.0, pois=(a_poi(),)
            ),
        )
        self._error = error
        self.legs: list[RouteLeg] = []

    async def run(self, leg, categories, **kwargs):
        self.legs.append(leg)
        for step in self._steps:
            yield step
        if self._error is not None:
            raise self._error


def client_for(store: InMemoryTripStore, pipeline: object | None) -> TestClient:
    app = create_app(RoutingSettings(offline=True), trip_store=store)
    app.state.discovery = pipeline
    return TestClient(app)


async def seeded(trip: Trip | None = None) -> InMemoryTripStore:
    store = InMemoryTripStore()
    await store.create(trip or routed_trip())
    return store


def lines(response) -> list[Any]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


class TestTheStream:
    async def test_it_answers_ok(self):
        client = client_for(await seeded(), StubPipeline())
        assert client.post("/api/trips/oregon-backcountry/replan", json={}).status_code == 200

    async def test_the_media_type_is_ndjson(self):
        client = client_for(await seeded(), StubPipeline())
        response = client.post("/api/trips/oregon-backcountry/replan", json={})
        assert response.headers["content-type"].startswith("application/x-ndjson")

    async def test_every_line_is_a_replan_event(self):
        client = client_for(await seeded(), StubPipeline())
        events = lines(client.post("/api/trips/oregon-backcountry/replan", json={}))
        assert all("stage" in event and "message" in event for event in events)

    async def test_each_event_is_on_its_own_line(self):
        """The frontend splits on newlines and is tested against chunk boundaries mid-line."""
        client = client_for(await seeded(), StubPipeline())
        body = client.post("/api/trips/oregon-backcountry/replan", json={}).text
        assert body.endswith("\n")
        assert all(json.loads(line) for line in body.splitlines() if line.strip())

    async def test_no_line_contains_a_raw_newline(self):
        """A POI note carrying a newline would split one event across two lines."""
        noisy = DiscoveryProgress(
            stage="done", message="line one\nline two", progress=1.0, pois=(a_poi(),)
        )
        client = client_for(await seeded(), StubPipeline(noisy))
        body = client.post("/api/trips/oregon-backcountry/replan", json={}).text
        assert len([line for line in body.splitlines() if line.strip()]) == 1

    async def test_the_final_event_is_done(self):
        client = client_for(await seeded(), StubPipeline())
        events = lines(client.post("/api/trips/oregon-backcountry/replan", json={}))
        assert events[-1]["stage"] == "done"

    async def test_pois_reach_the_client(self):
        """The whole point: pins had no data source."""
        client = client_for(await seeded(), StubPipeline())
        events = lines(client.post("/api/trips/oregon-backcountry/replan", json={}))
        assert events[-1]["pois"][0]["name"] == "Halfway Flat"

    async def test_a_streamed_poi_is_verified(self):
        """Nothing unresolved may reach the map; `Poi` enforces it, this confirms it."""
        client = client_for(await seeded(), StubPipeline())
        events = lines(client.post("/api/trips/oregon-backcountry/replan", json={}))
        assert events[-1]["pois"][0]["place_id"]


class TestWhatIsCheckedBeforeTheStreamOpens:
    """After the first byte the status is committed, so these must be answered up front."""

    async def test_a_missing_trip_is_a_json_404(self):
        client = client_for(InMemoryTripStore(), StubPipeline())
        response = client.post("/api/trips/no-such-trip/replan", json={})
        assert response.status_code == 404
        assert response.json()["code"] == "trip_not_found"

    async def test_an_invalid_slug_is_a_json_400(self):
        client = client_for(await seeded(), StubPipeline())
        assert client.post("/api/trips/UPPER/replan", json={}).status_code == 400

    async def test_no_credentials_is_a_json_501(self):
        """Distinguishable from broken, which is what the frontend switches on."""
        client = client_for(await seeded(), None)
        response = client.post("/api/trips/oregon-backcountry/replan", json={})
        assert response.status_code == 501
        assert response.json()["code"] == "not_implemented"

    async def test_an_unrouted_trip_is_refused_before_streaming(self):
        """Searching a corridor that does not exist would spend metered requests on nothing."""
        unrouted = Trip(
            slug="oregon-backcountry",
            name="x",
            created_at=T0,
            edited_at=T0,
            waypoints=(
                Waypoint(coordinate=Coordinate(lat=45.0, lon=-121.0)),
                Waypoint(coordinate=Coordinate(lat=46.0, lon=-121.0)),
            ),
            legs=(TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1),),
        )
        client = client_for(await seeded(unrouted), StubPipeline())
        response = client.post("/api/trips/oregon-backcountry/replan", json={})
        assert response.status_code == 422
        assert response.json()["code"] == "route_incomplete"


class TestWhichLegIsSearched:
    async def test_the_longest_routed_leg_is_used(self):
        """A trip is often one long ride and a short connector; searching the connector
        would look at the wrong half of the map."""
        pipeline = StubPipeline()
        trip = routed_trip(leg(distance_m=2_000.0), leg(distance_m=90_000.0))
        client = client_for(await seeded(trip), pipeline)
        client.post("/api/trips/oregon-backcountry/replan", json={})
        assert pipeline.legs[0].distance_m == 90_000.0


class TestFailureInsideTheStream:
    async def test_an_unexpected_error_still_ends_with_done(self):
        """The status is already 200. A truncated body is indistinguishable from a dropped
        connection; a final `done` event is not."""
        client = client_for(await seeded(), StubPipeline(error=RuntimeError("boom")))
        events = lines(client.post("/api/trips/oregon-backcountry/replan", json={}))
        assert events[-1]["stage"] == "done"

    async def test_the_failure_does_not_leak_the_exception(self):
        """Same disclosure rule as the tool layer: this reaches an unauthenticated browser."""
        client = client_for(
            await seeded(),
            StubPipeline(error=RuntimeError("/home/tim/.config/creds.json missing")),
        )
        body = client.post("/api/trips/oregon-backcountry/replan", json={}).text
        assert "/home/tim" not in body

    async def test_events_before_the_failure_are_still_delivered(self):
        """Partial results are the point of streaming; losing them to a late error is not."""
        client = client_for(await seeded(), StubPipeline(error=RuntimeError("boom")))
        events = lines(client.post("/api/trips/oregon-backcountry/replan", json={}))
        assert any(event["stage"] == "discovery" for event in events)
