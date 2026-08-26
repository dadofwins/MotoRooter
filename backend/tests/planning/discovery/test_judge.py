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
        """Scoring must not become a fan-out multiplier, and a model comparing places against
        each other ranks them better than one seeing each alone.

        Scripted with a *usable* reply on purpose: an empty one is now retried, so this would
        count two calls for reasons that have nothing to do with the batch.
        """
        _, client = judge(says({"scores": [{"index": 0, "score": 0.8, "reason": "good"}]}))
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


class TestAnUnusableReplyIsRetried:
    """The judge intermittently returns nothing usable, and it is expensive when it does.

    Measured across four live runs of one corridor: three produced zero POIs from five to
    eight resolved, on-route candidates. Every search, extraction and Places lookup had
    already been paid for.

    What it is not: batch size — twenty candidates score fine in 24s — and not the timeout,
    since the failing runs finished in 32s against a 45s budget. It has resisted
    reproduction, and chasing an intermittent cause is worth less than surviving it: this is
    one call, it works most of the time, and a second attempt costs one request against a
    corridor's worth of work already spent.

    Retried only when the reply yields *nothing*. A partial answer is a judgement — the model
    declining to score one place — and asking again would discard the scores it did give.
    """

    @staticmethod
    def _scores(count: int) -> str:
        import json as _json

        return _json.dumps(
            {"scores": [{"index": i, "score": 0.8, "reason": "good"} for i in range(count)]}
        )

    async def test_an_empty_reply_is_asked_again(self):
        client = FakeLlmClient(
            replies=(
                AssistantMessage(content='{"scores": []}'),
                AssistantMessage(content=self._scores(2)),
            )
        )
        scored = await CandidateJudge(client).judge(
            tuple(resolved(f"Place {i}") for i in range(2)), LEG
        )
        assert len(scored) == 2
        assert client.call_count == 2

    async def test_prose_is_asked_again(self):
        """The documented failure: a model answering with prose instead of JSON."""
        client = FakeLlmClient(
            replies=(
                AssistantMessage(content="Sure! Here are my thoughts on these places..."),
                AssistantMessage(content=self._scores(1)),
            )
        )
        assert await CandidateJudge(client).judge(
            tuple(resolved(f"Place {i}") for i in range(1)), LEG
        )
        assert client.call_count == 2

    async def test_it_gives_up_after_one_retry(self):
        """Two attempts, not a loop. A model returning prose twice will return it again, and
        the run has other things to finish."""
        client = FakeLlmClient(
            replies=(AssistantMessage(content="no json here"),), repeat_last=True
        )
        assert (
            await CandidateJudge(client).judge(tuple(resolved(f"Place {i}") for i in range(2)), LEG)
            == ()
        )
        assert client.call_count == 2

    async def test_a_partial_answer_is_not_retried(self):
        """Declining to score one place is an opinion. Asking again would throw away the
        scores it did give, to no purpose."""
        client = FakeLlmClient(
            replies=(AssistantMessage(content=self._scores(1)),), repeat_last=True
        )
        scored = await CandidateJudge(client).judge(
            tuple(resolved(f"Place {i}") for i in range(3)), LEG
        )
        assert len(scored) == 1
        assert client.call_count == 1

    async def test_a_good_answer_is_not_retried(self):
        client = FakeLlmClient(
            replies=(AssistantMessage(content=self._scores(2)),), repeat_last=True
        )
        await CandidateJudge(client).judge(tuple(resolved(f"Place {i}") for i in range(2)), LEG)
        assert client.call_count == 1

    async def test_nothing_to_score_asks_nothing(self):
        client = FakeLlmClient(replies=(AssistantMessage(content="{}"),), repeat_last=True)
        assert await CandidateJudge(client).judge((), LEG) == ()
        assert client.call_count == 0


class TestAnUnusableReplyIsRecorded:
    """Log the reply when scoring produces nothing, rather than waiting to catch one.

    Chasing a bug that resists reproduction while declining to record it when it happens is
    the expensive order. The retry reduced how often this is seen; it did not explain it, and
    each unexplained occurrence still throws away a corridor of searches and lookups.

    Server-side only, which is the policy Tim set for exactly this: log the raw thing, send
    the sanitised thing to the model.
    """

    @staticmethod
    def _judge_with(reply: str, caplog):
        import logging

        caplog.set_level(logging.WARNING, logger="motorooter.planning.discovery.judge")
        client = FakeLlmClient(replies=(AssistantMessage(content=reply),), repeat_last=True)
        return client

    async def test_the_reply_is_logged_when_nothing_scores(self, caplog):
        client = self._judge_with("I think these are all quite nice places really.", caplog)
        await CandidateJudge(client).judge([resolved("A")], LEG)
        assert "quite nice places" in caplog.text

    async def test_it_is_logged_at_warning(self, caplog):
        client = self._judge_with("not json", caplog)
        await CandidateJudge(client).judge([resolved("A")], LEG)
        assert any(record.levelname == "WARNING" for record in caplog.records)

    async def test_it_says_how_many_it_was_asked_about(self, caplog):
        client = self._judge_with("not json", caplog)
        await CandidateJudge(client).judge([resolved("A"), resolved("B")], LEG)
        assert "2" in caplog.text

    async def test_a_long_reply_is_truncated(self, caplog):
        """A model that answers with an essay should not fill the log with it."""
        from motorooter.planning.discovery.judge import MAX_LOGGED_REPLY_CHARS

        client = self._judge_with("x" * 10_000, caplog)
        await CandidateJudge(client).judge([resolved("A")], LEG)
        assert len(caplog.text) < MAX_LOGGED_REPLY_CHARS * 3

    async def test_a_good_reply_logs_nothing(self, caplog):
        client = self._judge_with(
            json.dumps({"scores": [{"index": 0, "score": 0.8, "reason": "good"}]}), caplog
        )
        await CandidateJudge(client).judge([resolved("A")], LEG)
        assert not caplog.records
