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

    def build(model: FakeLlmClient | None, *, seed: Trip | None = None, discovery=None):
        store = InMemoryTripStore()
        # Seeded directly rather than through POST /api/trips: a trip created that way has
        # no waypoints, and the editing tools are only interesting on one that has some.
        asyncio.run(store.create(seed or a_trip()))
        app = create_app(RoutingSettings(offline=True), trip_store=store)
        app.state.chat_model = model
        # Offline builds no Places client, and `add_waypoint` now needs one: a name is
        # resolved to a real place before anything is pinned, which is the point of it.
        app.state.place_lookup = _StubLookup()
        app.state.discovery = discovery
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

    def test_only_the_event_that_changed_it_says_so(self, client_with):
        """A read-only event after an edit reports false; `done` still reports true.

        The flag answers two questions and they are not the same one: "re-read now, this
        event moved the document", and "did anything move during this turn", which is what a
        client reading only the tail needs. Sticky on every event made the first answer wrong
        — a client doing exactly what the schema says re-read the document once per remaining
        event in the turn.
        """
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
                AssistantMessage(content="Added. Anything else?"),
            )
        )
        stream = events(post(client))
        changed = [event["kind"] for event in stream if event["trip_changed"]]

        assert "tool_finished" in changed, "the event that moved the document must say so"
        assert "done" in changed, "a client reading only the tail must still learn it changed"
        assert "message" not in changed, "a read-only event after an edit must not repeat it"

    def test_a_turn_that_changed_nothing_says_so_on_every_event(self, client_with):
        """The other direction, so the fix cannot be 'always false except done'."""
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


class TestProgressInsideATurn:
    """A chat turn running find_places was silent for thirty seconds.

    `tool_started` then nothing until `tool_finished`, while the same work from the Replan
    button reports fifteen or twenty events. The information existed; the transport had
    nowhere to put it.

    Exercised through the real `find_places` against a stub pipeline rather than a test-only
    tool, so what is under test is the path a rider takes. A hook in production code that
    exists only for a test is the thing this project keeps deleting.
    """

    class StubPipeline:
        """Emits the shape the real pipeline emits, without the four APIs."""

        async def run(self, leg, categories, **kwargs):
            from motorooter.planning.discovery.pipeline import DiscoveryProgress

            yield DiscoveryProgress(
                stage="discovery", message="searching near Cle Elum", progress=0.25
            )
            yield DiscoveryProgress(stage="enrichment", message="scoring 3/8 places", progress=0.75)
            yield DiscoveryProgress(stage="done", message="3 worth showing", progress=1.0, pois=())

    @staticmethod
    def _routed_trip() -> Trip:
        from motorooter.routing.models import RouteLeg

        now = utc_now()
        leg = RouteLeg(
            geometry=(
                Coordinate(lat=47.0, lon=-121.0),
                Coordinate(lat=47.1, lon=-121.1),
            ),
            distance_m=8000.0,
            duration_s=600.0,
            provider="fake",
            intent=LegIntent.TWISTY_PAVED,
        )
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
                TripLeg(
                    intent=LegIntent.TWISTY_PAVED,
                    start_waypoint_index=0,
                    end_waypoint_index=1,
                    routed=leg,
                ),
            ),
        )

    def _events(self, client_with):
        model = says(
            AssistantMessage(
                content=None,
                tool_calls=(
                    ToolCall(id="c1", name="find_places", arguments='{"categories": ["food"]}'),
                ),
            ),
            AssistantMessage(content="Found three places."),
        )
        client, _ = client_with(model, seed=self._routed_trip(), discovery=self.StubPipeline())
        return events(post(client))

    def test_progress_reaches_the_client(self, client_with):
        kinds = [event["kind"] for event in self._events(client_with)]
        assert "tool_progress" in kinds

    def test_it_arrives_before_the_tool_finishes(self, client_with):
        kinds = [event["kind"] for event in self._events(client_with)]
        assert kinds.index("tool_progress") < kinds.index("tool_finished")

    def test_it_carries_the_message_and_fraction(self, client_with):
        progress = [e for e in self._events(client_with) if e["kind"] == "tool_progress"]
        assert progress[0]["message"] == "searching near Cle Elum"
        assert progress[0]["progress"] == pytest.approx(0.25)

    def test_it_names_the_tool(self, client_with):
        """So a client can associate it without inferring from ordering — which breaks the
        moment a turn runs two tools and one of them is slow."""
        progress = [e for e in self._events(client_with) if e["kind"] == "tool_progress"]
        assert {e["tool"] for e in progress} == {"find_places"}

    def test_other_events_carry_no_fraction(self, client_with):
        """`progress` is null except on a progress event; a client must not read it as zero."""
        others = [e for e in self._events(client_with) if e["kind"] != "tool_progress"]
        assert all(event["progress"] is None for event in others)


class TestALoopUnderConstruction:
    """A trip that starts and ends in the same town, at the moment it has only that town.

    Tim's report: "three days of dirt starting and ending in Leavenworth" produced a
    pydantic validation report in the rail. The model adds the start, adds the end, and
    between those two calls the trip is `[Leavenworth, Leavenworth]` — a leg whose ends are
    one coordinate. That is an ordinary step in building a loop, so the rider must see
    nothing at all: not a warning, not a failed tool, not a chat line.
    """

    def a_trip_at(self, coordinate: Coordinate) -> Trip:
        now = utc_now()
        return Trip(
            slug=SLUG,
            name="Leavenworth Loop",
            created_at=now,
            edited_at=now,
            waypoints=(Waypoint(coordinate=coordinate, name="Leavenworth"),),
            legs=(),
        )

    def adds_the_same_place_again(self) -> FakeLlmClient:
        return says(
            AssistantMessage(
                content=None,
                tool_calls=(
                    ToolCall(id="c1", name="add_waypoint", arguments='{"name": "Leavenworth"}'),
                ),
            ),
            AssistantMessage(content="Where would you like to ride in between?"),
        )

    def stream(self, client_with):
        # _StubLookup resolves any name to this coordinate, so adding it to a trip already
        # sitting on it is exactly the coincident span.
        client, _ = client_with(
            self.adds_the_same_place_again(),
            seed=self.a_trip_at(Coordinate(lat=47.34, lon=-120.58)),
        )
        return events(post(client, "three days of dirt starting and ending in Leavenworth"))

    def test_the_turn_completes(self, client_with):
        assert self.stream(client_with)[-1]["kind"] == "done"

    def test_no_tool_fails(self, client_with):
        assert not [event for event in self.stream(client_with) if event["kind"] == "tool_failed"]

    def test_the_rider_is_told_nothing_about_it(self, client_with):
        """Silent means silent. A message here would fire during ordinary loop-building."""
        messages = " ".join(event["message"] for event in self.stream(client_with))
        assert "could not be routed" not in messages
        assert "pydantic" not in messages
        assert "degenerate" not in messages

    def test_the_waypoint_is_added_rather_than_refused(self, client_with):
        client, store = client_with(
            self.adds_the_same_place_again(),
            seed=self.a_trip_at(Coordinate(lat=47.34, lon=-120.58)),
        )
        post(client, "three days of dirt starting and ending in Leavenworth")
        assert len(asyncio.run(store.get(SLUG)).waypoints) == 2

    def test_the_engine_was_never_asked(self, client_with):
        """The assertion that discriminates, on the stack the app actually builds.

        The other cases here would pass without the guard, because offline routing is
        `FakeProvider` and it answers a coincident span happily — it is the ORS reply that
        is degenerate, not the request. What no engine can produce is a two-point leg:
        `FakeProvider` interpolates nine across this span, so two means the request was
        answered above it and nothing was routed.
        """
        client, store = client_with(
            self.adds_the_same_place_again(),
            seed=self.a_trip_at(Coordinate(lat=47.34, lon=-120.58)),
        )
        post(client, "three days of dirt starting and ending in Leavenworth")
        legs = asyncio.run(store.get(SLUG)).legs
        assert [leg.routed is not None for leg in legs] == [True]
        assert len(legs[0].routed.geometry) == 2
