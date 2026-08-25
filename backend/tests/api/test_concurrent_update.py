"""PUT /api/trips/{slug} under concurrent writers.

The bug these pin down: the handler read the trip, merged a partial request into it, and
wrote the whole document back with no precondition. Two clients editing *different* fields of
the same public, world-editable trip meant the second write rolled the first one's fields
back to the state both had read — and the loser got a 200 showing their data saved.

`UpdateTripRequest` is a partial update, so a conflict is usually resolvable: re-read, merge
the same fields onto the newer document, and the result is the union of both edits. Only a
writer that loses twice gets a 409.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from motorooter.api.error_codes import ErrorCode
from motorooter.app import create_app
from motorooter.routing.factory import RoutingSettings
from motorooter.routing.models import Coordinate
from motorooter.trips.models import Trip, Waypoint
from motorooter.trips.store import InMemoryTripStore, VersionedTrip

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

RIVAL_WAYPOINTS = tuple(Waypoint(coordinate=Coordinate(lat=47.0 + i, lon=-120.0)) for i in range(3))


class InterferingStore(InMemoryTripStore):
    """Simulates another writer landing between a caller's read and its write.

    `interfere` is applied on `get_versioned` — precisely the window the precondition
    exists to close. Set `always` to keep interfering, for the lose-twice case.
    """

    def __init__(self, *, always: bool = False) -> None:
        super().__init__()
        self.rival_edit: dict[str, object] | None = None
        self.always = always
        self.conflicts = 0

    async def get_versioned(self, slug: str) -> VersionedTrip:
        versioned = await super().get_versioned(slug)
        if self.rival_edit is not None:
            await super().put(versioned.trip.model_copy(update=self.rival_edit))
            if not self.always:
                self.rival_edit = None
        return versioned

    async def put(self, trip: Trip, *, if_version: int | None = None) -> Trip:
        try:
            return await super().put(trip, if_version=if_version)
        except Exception:
            self.conflicts += 1
            raise


@pytest.fixture
def store():
    return InterferingStore()


def make_client(store: InMemoryTripStore) -> TestClient:
    return TestClient(create_app(RoutingSettings(offline=True), trip_store=store))


@pytest.fixture
def client(store):
    return make_client(store)


@pytest.fixture
def trip(client):
    return client.post("/api/trips", json={"name": "Oregon Backcountry"}).json()


def waypoints(n: int = 2):
    return [{"coordinate": {"lat": 45.0 + i, "lon": -121.0}, "pinned": True} for i in range(n)]


class TestLostUpdate:
    def test_a_rival_edit_to_other_fields_is_not_rolled_back(self, client, store, trip):
        """The original bug, end to end.

        Rider A renames while rider B adds waypoints. A must not revert B's waypoints.
        """
        store.rival_edit = {"waypoints": RIVAL_WAYPOINTS}

        response = client.put(f"/api/trips/{trip['slug']}", json={"name": "Renamed"})

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Renamed", "this writer's edit must land"
        assert len(body["waypoints"]) == 3, "the rival's waypoints must survive"

    def test_the_conflict_is_actually_detected(self, client, store, trip):
        """Guards the test above: it must pass via the precondition, not by luck."""
        store.rival_edit = {"name": "Rival"}
        client.put(f"/api/trips/{trip['slug']}", json={"name": "Mine"})
        assert store.conflicts == 1

    def test_an_uncontended_update_does_not_retry(self, client, store, trip):
        client.put(f"/api/trips/{trip['slug']}", json={"name": "Renamed"})
        assert store.conflicts == 0

    def test_losing_twice_is_a_409(self):
        """A permanently contended trip must report a conflict rather than retry forever."""
        store = InterferingStore(always=True)
        client = make_client(store)
        created = client.post("/api/trips", json={"name": "Contended"}).json()
        store.rival_edit = {"name": "Rival"}

        response = client.put(f"/api/trips/{created['slug']}", json={"name": "Mine"})

        assert response.status_code == 409
        assert response.json()["code"] == ErrorCode.TRIP_MODIFIED_CONCURRENTLY.value

    def test_retries_are_bounded(self):
        """Two attempts, not an unbounded loop against a hot trip."""
        store = InterferingStore(always=True)
        client = make_client(store)
        created = client.post("/api/trips", json={"name": "Contended"}).json()
        store.rival_edit = {"name": "Rival"}

        client.put(f"/api/trips/{created['slug']}", json={"name": "Mine"})

        assert store.conflicts == 2

    def test_deletion_mid_update_is_a_404_not_a_resurrection(self, client, trip):
        """Writing anyway would recreate a trip somebody deliberately removed."""
        client.delete(f"/api/trips/{trip['slug']}")
        assert client.put(f"/api/trips/{trip['slug']}", json={"name": "x"}).status_code == 404


class TestExistingBehaviourPreserved:
    def test_rename_still_does_not_mark_discovery_stale(self, client, trip):
        before = client.get(f"/api/trips/{trip['slug']}").json()["edited_at"]
        after = client.put(f"/api/trips/{trip['slug']}", json={"name": "New"}).json()
        assert after["edited_at"] == before

    def test_geometry_change_still_advances_edited_at(self, client, trip):
        before = client.get(f"/api/trips/{trip['slug']}").json()["edited_at"]
        after = client.put(f"/api/trips/{trip['slug']}", json={"waypoints": waypoints()})
        assert after.json()["edited_at"] > before

    def test_validation_still_rejects_non_contiguous_legs(self, client, trip):
        body = {
            "waypoints": waypoints(4),
            "legs": [
                {"intent": "unpaved", "start_waypoint_index": 0, "end_waypoint_index": 1},
                {"intent": "unpaved", "start_waypoint_index": 2, "end_waypoint_index": 3},
            ],
        }
        assert client.put(f"/api/trips/{trip['slug']}", json=body).status_code == 422
