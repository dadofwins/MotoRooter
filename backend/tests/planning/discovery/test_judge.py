"""Scoring, and what the model is and is not allowed to decide.

Everything measurable was measured before this stage. What is left is judgement, and the
guard is structural rather than instructed: only `score` and `reason` are read from the
reply, so a model cannot move a pin, rename a place or contradict a distance no matter what
it returns.
"""

import json
from math import pi

import pytest

from motorooter.llm.messages import AssistantMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.planning.discovery.judge import CandidateJudge
from motorooter.planning.discovery.models import Candidate, ResolvedCandidate
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import PoiCategory

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180


def north(metres: float) -> Coordinate:
    return Coordinate(lat=metres / M_PER_DEGREE_LAT, lon=0.0)


LEG = RouteLeg(
    geometry=tuple(north(index * 100.0) for index in range(101)),
    distance_m=10_000.0,
    duration_s=600.0,
    provider="fake",
    intent=LegIntent.UNPAVED,
)


def resolved(name: str = "Halfway Flat", *, snippet: str | None = None) -> ResolvedCandidate:
    return ResolvedCandidate(
        candidate=Candidate(
            name=name,
            category=PoiCategory.WILD_CAMP,
            found_near=north(5000.0),
            source="brave",
            snippet=snippet,
        ),
        place_id="ChIJ_x",
        coordinate=north(5000.0),
        distance_off_route_m=250.0,
        rating=4.4,
        user_rating_count=15,
    )


def says(payload: object) -> AssistantMessage:
    return AssistantMessage(content=json.dumps(payload))


def judge(*replies: AssistantMessage) -> tuple[CandidateJudge, FakeLlmClient]:
    client = FakeLlmClient(replies=replies)
    return CandidateJudge(client), client


class TestScoring:
    async def test_it_scores_a_candidate(self):
        scorer, _ = judge(says({"scores": [{"index": 0, "score": 0.8, "reason": "great spot"}]}))
        scored = await scorer.judge([resolved()], LEG)
        assert scored[0].score == pytest.approx(0.8)

    async def test_the_reason_is_kept(self):
        scorer, _ = judge(says({"scores": [{"index": 0, "score": 0.8, "reason": "great spot"}]}))
        assert (await scorer.judge([resolved()], LEG))[0].reason == "great spot"

    async def test_results_come_back_ranked(self):
        scorer, _ = judge(
            says(
                {
                    "scores": [
                        {"index": 0, "score": 0.2, "reason": "dull"},
                        {"index": 1, "score": 0.9, "reason": "superb"},
                    ]
                }
            )
        )
        scored = await scorer.judge([resolved("A"), resolved("B")], LEG)
        assert [item.score for item in scored] == [0.9, 0.2]

    async def test_the_evidence_is_attached_to_the_score(self):
        """So a human can check whether the judgement follows from the numbers."""
        scorer, _ = judge(says({"scores": [{"index": 0, "score": 0.8, "reason": "x"}]}))
        assert (await scorer.judge([resolved()], LEG))[0].evidence.distance_off_route_m == 250.0


class TestTheModelCannotChangeTheFacts:
    """Structural, not instructed: only score and reason are read from the reply."""

    async def test_a_returned_coordinate_is_ignored(self):
        scorer, _ = judge(
            says(
                {
                    "scores": [
                        {
                            "index": 0,
                            "score": 0.8,
                            "reason": "x",
                            "latitude": 0.0,
                            "longitude": 0.0,
                        }
                    ]
                }
            )
        )
        scored = await scorer.judge([resolved()], LEG)
        assert scored[0].resolved.coordinate == north(5000.0)

    async def test_a_returned_name_is_ignored(self):
        scorer, _ = judge(
            says({"scores": [{"index": 0, "score": 0.8, "reason": "x", "name": "Elsewhere"}]})
        )
        assert (await scorer.judge([resolved()], LEG))[0].resolved.candidate.name == "Halfway Flat"

    async def test_a_returned_distance_is_ignored(self):
        scorer, _ = judge(
            says(
                {"scores": [{"index": 0, "score": 0.8, "reason": "x", "distance_off_route_m": 99}]}
            )
        )
        assert (await scorer.judge([resolved()], LEG))[0].evidence.distance_off_route_m == 250.0


class TestTheEvidenceItIsGiven:
    async def test_measurements_are_in_the_prompt(self):
        _, client = judge(says({"scores": []}))
        await CandidateJudge(client).judge([resolved()], LEG)
        assert "250 m off route" in str(client.conversations[-1])

    async def test_the_places_rating_is_in_the_prompt(self):
        """A rating is a fact about the place, so the model is handed it rather than asked."""
        _, client = judge(says({"scores": []}))
        await CandidateJudge(client).judge([resolved()], LEG)
        assert "rated 4.4" in str(client.conversations[-1])

    async def test_the_snippet_is_in_the_prompt(self):
        """Local knowledge no metric produces: "washes out after spring melt"."""
        _, client = judge(says({"scores": []}))
        await CandidateJudge(client).judge([resolved(snippet="washes out after spring melt")], LEG)
        assert "washes out" in str(client.conversations[-1])

    async def test_an_unmeasured_signal_is_omitted_rather_than_shown_as_zero(self):
        """ "unpaved 0%" for an unsurveyed road invites reasoning about tarmac that may not
        be there."""
        _, client = judge(says({"scores": []}))
        await CandidateJudge(client).judge([resolved()], LEG)
        assert "0% unpaved" not in str(client.conversations[-1])

    async def test_one_call_for_the_whole_batch(self):
        _, client = judge(says({"scores": []}))
        await CandidateJudge(client).judge([resolved("A"), resolved("B")], LEG)
        assert client.call_count == 1


class TestAMisbehavingModel:
    @pytest.mark.parametrize(
        "reply",
        [
            AssistantMessage(content="I think they are all quite good."),
            AssistantMessage(content="{not json"),
            AssistantMessage(content=None),
            AssistantMessage(content='{"scores": "not-a-list"}'),
            AssistantMessage(content='{"scores": [null]}'),
            AssistantMessage(content='{"scores": [{}]}'),
        ],
    )
    async def test_a_malformed_reply_yields_nothing_rather_than_raising(self, reply):
        scorer, _ = judge(reply)
        assert await scorer.judge([resolved()], LEG) == ()

    @pytest.mark.parametrize("score", [1.5, -0.2, 11, "high", True, None])
    async def test_an_out_of_range_score_is_dropped_rather_than_clamped(self, score):
        """Clamping 11 to 1.0 makes a misunderstanding indistinguishable from a top mark."""
        scorer, _ = judge(says({"scores": [{"index": 0, "score": score, "reason": "x"}]}))
        assert await scorer.judge([resolved()], LEG) == ()

    async def test_a_score_with_no_reason_is_dropped(self):
        scorer, _ = judge(says({"scores": [{"index": 0, "score": 0.9}]}))
        assert await scorer.judge([resolved()], LEG) == ()

    async def test_an_empty_reason_is_dropped(self):
        scorer, _ = judge(says({"scores": [{"index": 0, "score": 0.9, "reason": "  "}]}))
        assert await scorer.judge([resolved()], LEG) == ()

    @pytest.mark.parametrize("index", [5, -1, "0"])
    async def test_a_bad_index_is_dropped(self, index):
        scorer, _ = judge(says({"scores": [{"index": index, "score": 0.9, "reason": "x"}]}))
        assert await scorer.judge([resolved()], LEG) == ()

    async def test_a_good_score_survives_alongside_a_bad_one(self):
        scorer, _ = judge(
            says(
                {
                    "scores": [
                        {"index": 0, "score": 11, "reason": "x"},
                        {"index": 1, "score": 0.7, "reason": "fine"},
                    ]
                }
            )
        )
        scored = await scorer.judge([resolved("A"), resolved("B")], LEG)
        assert [item.resolved.candidate.name for item in scored] == ["B"]

    async def test_an_empty_batch_calls_nothing(self):
        _, client = judge(says({"scores": []}))
        assert await CandidateJudge(client).judge([], LEG) == ()
        assert client.call_count == 0
