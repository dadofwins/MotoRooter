"""`POST /api/trips/{slug}/chat`.

The endpoint exists so nothing merges that cannot be exercised end to end. What it owes the
client is narrow and mostly about the stream: `done` is always last, and `truncated` on that
final event is the only way a client reading the tail can tell "finished" from "cut off
mid-task". A truncated run that reports itself as done is the chat equivalent of a discovery
run reporting "0 worth showing" after the judge returned nothing.
"""

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from motorooter.app import create_app
from motorooter.llm.messages import AssistantMessage, ToolCall
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.routing.factory import RoutingSettings
from motorooter.routing.models import Coordinate, LegIntent
from motorooter.trips.models import Trip, TripLeg, Waypoint, utc_now
from motorooter.trips.store import InMemoryTripStore

SLUG = "cascade-loop"


def a_trip() -> Trip:
    now = utc_now()
    return Trip(
        slug=SLUG,
        name="Cascade Loop",
        created_at=now,
        edited_at=now,
        waypoints=(
            Waypoint(coordinate=Coordinate(lat=47.0, lon=-121.0), name="Start"),
            Waypoint(coordinate=Coordinate(lat=47.1, lon=-121.1), name="End"),
        ),
        legs=(
            TripLeg(intent=LegIntent.TWISTY_PAVED, start_waypoint_index=0, end_waypoint_index=1),
        ),
    )


class _StubLookup:
    """Resolves any name to one real-looking place, so the endpoint test is about the
    stream rather than about Places."""

    async def search(self, text, *, near=None, limit=5):
        from motorooter.planning.discovery.lookup import FoundPlace

        return (
            FoundPlace(
                name="Blewett Pass",
                place_id="ChIJ_bp",
                coordinate=Coordinate(lat=47.34, lon=-120.58),
                kinds=("route",),
            ),
        )


def says(*replies: AssistantMessage) -> FakeLlmClient:
    return FakeLlmClient(replies=replies, repeat_last=True)


@pytest.fixture
def client_with():
    """Builds an app whose chat uses a scripted model."""

    def build(model: FakeLlmClient | None, *, seed: Trip | None = None):
        store = InMemoryTripStore()
        # Seeded directly rather than through POST /api/trips: a trip created that way has
        # no waypoints, and the editing tools are only interesting on one that has some.
        asyncio.run(store.create(seed or a_trip()))
        app = create_app(RoutingSettings(offline=True), trip_store=store)
        app.state.chat_model = model
        # Offline builds no Places client, and `add_waypoint` now needs one: a name is
        # resolved to a real place before anything is pinned, which is the point of it.
        app.state.place_lookup = _StubLookup()
        return TestClient(app), store

    return build


def events(response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def post(client, message: str = "how long is this trip?"):
    return client.post(f"/api/trips/{SLUG}/chat", json={"message": message})


class TestTheStream:
    def test_it_answers_two_hundred(self, client_with):
        client, _ = client_with(says(AssistantMessage(content="It is about 8 km.")))
        assert post(client).status_code == 200

    def test_it_is_newline_delimited_json(self, client_with):
        client, _ = client_with(says(AssistantMessage(content="It is about 8 km.")))
        response = post(client)
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert all(json.loads(line) for line in response.text.splitlines() if line.strip())

    def test_done_is_always_last(self, client_with):
        client, _ = client_with(says(AssistantMessage(content="It is about 8 km.")))
        assert events(post(client))[-1]["kind"] == "done"

    def test_the_assistant_text_is_streamed(self, client_with):
        client, _ = client_with(says(AssistantMessage(content="It is about 8 km.")))
        assert any(
            event["kind"] == "message" and "8 km" in event["message"]
            for event in events(post(client))
        )

    def test_a_finished_run_is_not_marked_truncated(self, client_with):
        client, _ = client_with(says(AssistantMessage(content="Done.")))
        assert events(post(client))[-1]["truncated"] is False


class TestToolCalls:
    def test_a_tool_call_is_reported_started_and_finished(self, client_with):
        client, _ = client_with(
            says(
                AssistantMessage(
                    content=None,
                    tool_calls=(ToolCall(id="c1", name="describe_trip", arguments="{}"),),
                ),
                AssistantMessage(content="It has two waypoints."),
            )
        )
        kinds = [event["kind"] for event in events(post(client))]
        assert "tool_started" in kinds
        assert "tool_finished" in kinds

    def test_an_unknown_tool_is_reported_not_fatal(self, client_with):
        """A model inventing a tool name is its mistake to correct, not a 500."""
        client, _ = client_with(
            says(
                AssistantMessage(
                    content=None,
                    tool_calls=(ToolCall(id="c1", name="teleport", arguments="{}"),),
                ),
                AssistantMessage(content="Sorry, I cannot do that."),
            )
        )
        stream = events(post(client))
        assert any(event["kind"] == "tool_failed" for event in stream)
        assert stream[-1]["kind"] == "done"

    def test_an_editing_tool_sets_trip_changed(self, client_with):
        """The client re-reads the document rather than reconstructing it from the stream."""
        client, _ = client_with(
            says(
                AssistantMessage(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id="c1",
                            name="add_waypoint",
                            arguments='{"name": "Blewett Pass"}',
                        ),
                    ),
                ),
                AssistantMessage(content="Added."),
            )
        )
        assert any(event["trip_changed"] for event in events(post(client)))

    def test_a_read_only_tool_does_not_set_trip_changed(self, client_with):
        client, _ = client_with(
            says(
                AssistantMessage(
                    content=None,
                    tool_calls=(ToolCall(id="c1", name="describe_trip", arguments="{}"),),
                ),
                AssistantMessage(content="Two waypoints."),
            )
        )
        assert not any(event["trip_changed"] for event in events(post(client)))


class TestBeingCutOff:
    def test_a_limit_marks_the_final_event_truncated(self, client_with):
        """A model that never stops calling tools. `repeat_last` keeps it going until the
        turn budget does, which is exactly the case a client must be able to detect."""
        client, _ = client_with(
            says(
                AssistantMessage(
                    content=None,
                    tool_calls=(ToolCall(id="c1", name="describe_trip", arguments="{}"),),
                )
            )
        )
        stream = events(post(client))
        assert stream[-1]["kind"] == "done"
        assert stream[-1]["truncated"] is True


class TestWhenItIsNotAvailable:
    def test_no_model_configured_answers_501(self, client_with):
        """Chat needs an OpenAI key. Without one the rest of the app still works, which is
        the same choice discovery makes."""
        client, _ = client_with(None)
        assert post(client).status_code == 501

    def test_an_unknown_trip_is_404(self, client_with):
        client, _ = client_with(says(AssistantMessage(content="hi")))
        assert (
            client.post("/api/trips/no-such-trip/chat", json={"message": "hi"}).status_code == 404
        )
