"""`POST /api/trips/{slug}/blurb`.

The rail's header line. Everything here is decoration: the endpoint's contract with the
client is that it answers, never that it answers with something. A client that gets `null`
keeps the static header, and so does one that gets 501 on a deployment with no OpenAI key.

The case worth guarding hardest is a trip with no chat history at all. This must not become
a chat feature — a rider who builds a trip entirely with the mouse gets a blurb too, and if
that ever stops being true the rule "chat is an accelerator, never a requirement" is broken
by a feature nobody meant to gate.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from motorooter.app import create_app
from motorooter.blurb.writer import BlurbWriter
from motorooter.llm.errors import LlmUnavailable
from motorooter.llm.messages import AssistantMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.routing.factory import RoutingSettings
from motorooter.routing.models import Coordinate, LegIntent
from motorooter.trips.models import Trip, TripLeg, Waypoint, utc_now
from motorooter.trips.store import InMemoryTripStore

SLUG = "leavenworth-loop"


def a_trip() -> Trip:
    now = utc_now()
    return Trip(
        slug=SLUG,
        name="Leavenworth Loop",
        created_at=now,
        edited_at=now,
        waypoints=(
            Waypoint(coordinate=Coordinate(lat=47.5962, lon=-120.6615), name="Leavenworth"),
            Waypoint(coordinate=Coordinate(lat=47.34, lon=-120.58), name="Blewett Pass"),
        ),
        legs=(TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1),),
    )


def says(line: str) -> FakeLlmClient:
    return FakeLlmClient(replies=(AssistantMessage(content=line),), repeat_last=True)


@pytest.fixture
def client_with():
    def build(model: FakeLlmClient | None, *, seed: Trip | None = None):
        store = InMemoryTripStore()
        asyncio.run(store.create(seed or a_trip()))
        app = create_app(RoutingSettings(offline=True), trip_store=store)
        app.state.blurb_writer = BlurbWriter(model) if model is not None else None
        return TestClient(app), store

    return build


def post(client, body: dict[str, object] | None = None):
    return client.post(f"/api/trips/{SLUG}/blurb", json=body if body is not None else {})


class TestWithoutAModel:
    def test_it_answers_501_rather_than_pretending(self, client_with):
        """Same gate as chat: no OpenAI key disables the feature, not the deployment."""
        client, _ = client_with(None)
        assert post(client).status_code == 501


class TestTheHappyPath:
    def test_it_returns_the_line(self, client_with):
        client, _ = client_with(says("sick dirt loop out of leavenworth"))
        assert post(client).json()["blurb"] == "sick dirt loop out of leavenworth"

    def test_it_is_two_hundred(self, client_with):
        client, _ = client_with(says("rad loop"))
        assert post(client).status_code == 200


class TestWithoutAnyChatHistory:
    """The case that decides whether this is a chat feature. It must not be one."""

    def test_an_empty_body_still_produces_a_blurb(self, client_with):
        client, _ = client_with(says("sick dirt loop out of leavenworth"))
        assert post(client).json()["blurb"]

    def test_history_is_optional_in_the_contract(self, client_with):
        """A client that never opened the rail sends no transcript, and that is normal."""
        client, _ = client_with(says("rad loop"))
        assert post(client, {}).status_code == 200


class TestWithHistory:
    def test_recent_turns_reach_the_model(self, client_with):
        model = says("go find a swimming hole")
        client, _ = client_with(model)
        post(
            client,
            {"history": [{"role": "user", "content": "anywhere to swim near here?"}]},
        )
        told = "\n".join(m.content or "" for m in model.conversations[-1])
        assert "anywhere to swim near here?" in told


class TestFailureIsNeverAnError:
    def test_an_upstream_failure_is_a_null_blurb_not_a_five_hundred(self, client_with):
        """The rail keeps its static header. Nothing reaches the rider."""
        client, _ = client_with(FakeLlmClient(error=LlmUnavailable("502")))
        response = post(client)
        assert response.status_code == 200
        assert response.json()["blurb"] is None

    def test_an_unusable_reply_is_a_null_blurb(self, client_with):
        client, _ = client_with(says("   "))
        assert post(client).json()["blurb"] is None


class TestAwkwardTrips:
    def test_an_empty_trip_does_not_five_hundred(self, client_with):
        """The frontend will not call it for one. An endpoint that 500s is a trap anyway."""
        empty = Trip(slug=SLUG, name="New", created_at=utc_now(), edited_at=utc_now())
        client, _ = client_with(says("nothing here yet, drop a pin"), seed=empty)
        assert post(client).status_code == 200

    def test_an_unknown_trip_is_a_404(self, client_with):
        client, _ = client_with(says("rad"))
        assert client.post("/api/trips/no-such-trip/blurb", json={}).status_code == 404
