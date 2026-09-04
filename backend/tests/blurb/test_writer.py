"""Writing the line.

The model's only job is voice. Every fact it may use is handed to it, so the tests here are
mostly about what it is told and what happens when it misbehaves — a rail header is
decoration, and nothing about it may reach a rider as an error.
"""

import pytest

from motorooter.blurb.facts import facts_for
from motorooter.blurb.models import Turn
from motorooter.blurb.writer import BlurbWriter
from motorooter.llm.errors import LlmQuotaExceeded, LlmUnavailable
from motorooter.llm.messages import AssistantMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg, Surface, SurfaceSpan
from motorooter.trips.models import Poi, PoiCategory, PoiSource, Trip, TripLeg, Waypoint, utc_now


def says(line: str) -> FakeLlmClient:
    return FakeLlmClient(replies=(AssistantMessage(content=line),), repeat_last=True)


def a_loop() -> Trip:
    now = utc_now()
    leg = RouteLeg(
        geometry=(Coordinate(lat=47.59, lon=-120.66), Coordinate(lat=47.34, lon=-120.58)),
        distance_m=100_000.0,
        duration_s=7_200.0,
        surface_spans=(SurfaceSpan(start_index=0, end_index=1, surface=Surface.UNPAVED),),
        provider="ors",
        intent=LegIntent.UNPAVED,
    )
    return Trip(
        slug="leavenworth-loop",
        name="Leavenworth Loop",
        created_at=now,
        edited_at=now,
        waypoints=(
            Waypoint(coordinate=Coordinate(lat=47.59, lon=-120.66), name="Leavenworth"),
            Waypoint(coordinate=Coordinate(lat=47.34, lon=-120.58), name="Blewett Pass"),
        ),
        legs=(
            TripLeg(
                intent=LegIntent.UNPAVED,
                start_waypoint_index=0,
                end_waypoint_index=1,
                routed=leg,
            ),
        ),
        pois=(
            Poi(
                id="halfway-flat",
                name="Halfway Flat",
                category=PoiCategory.WILD_CAMP,
                coordinate=Coordinate(lat=47.5, lon=-120.6),
                source=PoiSource.PLACES,
            ),
        ),
    )


def sent(model: FakeLlmClient) -> str:
    """Everything the model was told, as one string."""
    return "\n".join(message.content or "" for message in model.conversations[-1])


class TestWhatTheModelIsTold:
    async def test_it_is_given_the_measured_figures(self):
        """Evidence, not invention: the numbers exist so it never has to make one up."""
        model = says("gnarly little dirt loop")
        await BlurbWriter(model).write(a_loop())
        told = sent(model)
        assert "Leavenworth" in told
        assert "Blewett Pass" in told
        assert "Halfway Flat" in told

    async def test_it_is_told_the_trip_is_a_loop(self):
        model = says("rad loop")
        await BlurbWriter(model).write(a_loop())
        assert "loop" in sent(model).lower()

    async def test_it_is_told_all_three_surface_shares(self):
        """Unsurveyed is not paved, here as everywhere else."""
        model = says("dirt")
        await BlurbWriter(model).write(a_loop())
        assert "unsurveyed" in sent(model).lower()

    async def test_it_is_forbidden_from_inventing_a_figure(self):
        model = says("sick")
        await BlurbWriter(model).write(a_loop())
        assert "never" in sent(model).lower()

    async def test_history_reaches_the_model_when_there_is_some(self):
        model = says("go find a swimming hole")
        await BlurbWriter(model).write(
            a_loop(), history=(Turn(role="user", content="somewhere to swim?"),)
        )
        assert "somewhere to swim?" in sent(model)


class TestNoChatHistory:
    """The case that decides whether this is a chat feature. It must not be one.

    A rider who builds the whole trip with the mouse gets a blurb; if this needed history,
    the feature would quietly become a reward for typing.
    """

    async def test_it_writes_a_line_with_no_history_at_all(self):
        assert await BlurbWriter(says("sick dirt loop out of leavenworth")).write(a_loop())

    async def test_it_still_has_the_trip_to_talk_about(self):
        model = says("rad")
        await BlurbWriter(model).write(a_loop())
        assert "Leavenworth" in sent(model)


class TestTheLineItself:
    async def test_it_returns_what_the_model_wrote(self):
        line = "sick dirt loop out of leavenworth — go find a swimming hole"
        assert await BlurbWriter(says(line)).write(a_loop()) == line

    async def test_surrounding_whitespace_is_trimmed(self):
        assert await BlurbWriter(says("  rad loop  ")).write(a_loop()) == "rad loop"

    async def test_a_multi_line_reply_is_collapsed_to_one(self):
        """The rail is one line. Two would be laid out by the browser, not by us."""
        written = await BlurbWriter(says("gnarly loop\nfind a camp")).write(a_loop())
        assert written == "gnarly loop find a camp"

    async def test_an_overlong_line_is_dropped_rather_than_cut_off(self):
        """A sentence severed mid-word looks like a bug; the static header does not."""
        assert await BlurbWriter(says("rad " * 200)).write(a_loop()) is None

    async def test_an_empty_reply_is_no_blurb_rather_than_an_empty_header(self):
        assert await BlurbWriter(says("   ")).write(a_loop()) is None

    async def test_a_reply_with_no_content_at_all_is_handled(self):
        model = FakeLlmClient(replies=(AssistantMessage(content=None),), repeat_last=True)
        assert await BlurbWriter(model).write(a_loop()) is None


class TestFailureIsAFallbackNotAnError:
    """Nothing here may reach a rider's face. The header keeps its static line instead."""

    @pytest.mark.parametrize(
        "failure",
        [LlmUnavailable("502"), LlmQuotaExceeded("spend cap")],
    )
    async def test_an_upstream_failure_returns_no_blurb(self, failure):
        assert await BlurbWriter(FakeLlmClient(error=failure)).write(a_loop()) is None


class TestAnEmptyTrip:
    """The frontend will not call it for an empty trip. The next caller might."""

    async def test_it_does_not_raise(self):
        empty = Trip(
            slug="new-trip",
            name="New Trip",
            created_at=utc_now(),
            edited_at=utc_now(),
        )
        model = says("start by dropping a pin somewhere gnarly")
        assert await BlurbWriter(model).write(empty) is not None

    async def test_the_model_is_told_the_trip_is_empty_rather_than_given_blanks(self):
        empty = Trip(
            slug="new-trip",
            name="New Trip",
            created_at=utc_now(),
            edited_at=utc_now(),
        )
        model = says("drop a pin")
        await BlurbWriter(model).write(empty)
        assert "nothing" in sent(model).lower() or "empty" in sent(model).lower()


class TestFactsAreTheOnlySource:
    def test_every_number_in_the_prompt_comes_from_the_facts(self):
        """The guard behind 'never state a number you were not given'.

        If the prompt could contain a figure the facts do not, the instruction would be
        unenforceable — so the evidence block is built from `TripFacts` and nothing else.
        """
        facts = facts_for(a_loop())
        assert facts.distance_km is not None
        assert facts.unsurveyed_share is not None


class TestTheModelIsNeverAskedToGuessACategory:
    """The join reaches the model, not just the facts.

    `facts` pairing a name with its category is worth nothing if the evidence block renders
    the two apart again, and that is exactly how this failed the first time. So the assertion
    is on what the model actually reads.
    """

    def a_trip_of_mixed_places(self) -> Trip:
        now = utc_now()
        return Trip(
            slug="mixed",
            name="Mixed",
            created_at=now,
            edited_at=now,
            pois=(
                Poi(
                    id="halfway-flat",
                    name="Halfway Flat",
                    category=PoiCategory.WILD_CAMP,
                    coordinate=Coordinate(lat=47.5, lon=-120.6),
                    source=PoiSource.PLACES,
                ),
                Poi(
                    id="diner",
                    name="South Cle Elum Diner",
                    category=PoiCategory.FOOD,
                    coordinate=Coordinate(lat=47.18, lon=-120.94),
                    source=PoiSource.PLACES,
                ),
            ),
        )

    async def test_every_name_reaches_the_model_beside_its_own_category(self):
        model = says("rad")
        await BlurbWriter(model).write(self.a_trip_of_mixed_places())
        told = sent(model)
        assert "Halfway Flat (wild_camp)" in told
        assert "South Cle Elum Diner (food)" in told

    async def test_a_bare_name_never_reaches_the_model(self):
        """A name with no category beside it is the shape that invited the guess."""
        model = says("rad")
        await BlurbWriter(model).write(self.a_trip_of_mixed_places())
        for line in sent(model).splitlines():
            if "Halfway Flat" in line:
                assert "wild_camp" in line
