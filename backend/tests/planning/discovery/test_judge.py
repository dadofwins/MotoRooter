"""Scoring, and what the model is and is not allowed to decide.

Everything measurable was measured before this stage. What is left is judgement, and the
guard is structural rather than instructed: only `score` and `reason` are read from the
reply, so a model cannot move a pin, rename a place or contradict a distance no matter what
it returns.
"""

import json
import logging
from math import pi

import pytest

from motorooter.llm.errors import LlmError, LlmUnavailable
from motorooter.llm.messages import AssistantMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.planning.discovery.judge import JUDGE_BATCH_SIZE, CandidateJudge
from motorooter.planning.discovery.models import Candidate, ResolvedCandidate
from motorooter.routing.geo import EARTH_RADIUS_M
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import PoiCategory

M_PER_DEGREE_LAT = EARTH_RADIUS_M * pi / 180
JUDGE_LOGGER = "motorooter.planning.discovery.judge"


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


def judge(*replies: AssistantMessage | LlmError) -> tuple[CandidateJudge, FakeLlmClient]:
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

    async def test_the_reason_is_told_not_to_quote_a_rating_or_a_review(self):
        """The reason is persisted on the trip; Places content may not be.

        A rating is legitimately *given* to the judge as evidence, so the boundary cannot be
        the prompt's input — it has to be what comes back. "Well-rated" is our
        characterisation; "4.6 from 59,117 ratings" is their field with prose around it.
        """
        _, client = judge(says({"scores": []}))
        await CandidateJudge(client).judge([resolved()], LEG)
        instruction = str(client.conversations[-1])
        assert "Do not put numeric ratings or quoted review text in the reason" in instruction

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


class TestOneBadEntryDoesNotCostTheBatch:
    """The judge-zero cause, caught by the logging rather than by reproduction.

    Two live captures, both the same shape — a quote in the wrong place in a key:

        {"index:3","score":0.50,"reason":"..."}
        {"index:2,"score":0.7,"reason":"..."}

    One of those in a reply makes `json.loads` fail on the *whole* thing, so twenty
    perfectly good scores were thrown away and the batch asked again. The retry is why this
    only ever showed as slowness. Salvaging the well-formed entries is the fix; repairing the
    broken one is not, because the damaged field is the index, and an index guessed wrong
    attaches a score to a different place — which is exactly the plausible-and-wrong failure
    every other stage here refuses.
    """

    @staticmethod
    def _reply(broken: str) -> AssistantMessage:
        return AssistantMessage(
            content=(
                '{"scores":['
                '{"index":0,"score":0.9,"reason":"first"},'
                f"{broken},"
                '{"index":2,"score":0.7,"reason":"third"}'
                "]}"
            )
        )

    @pytest.mark.parametrize(
        "broken",
        [
            '{"index:1","score":0.5,"reason":"quote after the colon"}',
            '{"index:1,"score":0.5,"reason":"quote swallowed the colon"}',
        ],
        ids=["captured-1", "captured-2"],
    )
    async def test_the_good_scores_in_a_broken_reply_survive(self, broken):
        scorer, _ = judge(self._reply(broken))
        scored = await scorer.judge([resolved("A"), resolved("B"), resolved("C")], LEG)
        assert [item.score for item in scored] == [0.9, 0.7]

    async def test_the_place_whose_entry_was_broken_is_simply_unscored(self):
        scorer, _ = judge(self._reply('{"index:1","score":0.5,"reason":"broken"}'))
        scored = await scorer.judge([resolved("A"), resolved("B"), resolved("C")], LEG)
        assert "B" not in [item.resolved.candidate.name for item in scored]

    async def test_it_does_not_ask_again_when_it_salvaged_something(self):
        """The retry exists for a reply that yielded nothing. Two good scores is not that."""
        scorer, client = judge(self._reply('{"index:1","score":0.5,"reason":"broken"}'))
        await scorer.judge([resolved("A"), resolved("B"), resolved("C")], LEG)
        assert len(client.conversations) == 1

    async def test_a_reply_that_is_not_json_at_all_still_yields_nothing(self):
        """Salvage must not turn prose into scores by finding braces in it."""
        scorer, _ = judge(
            AssistantMessage(content="I think the campsite {sic} is probably quite nice."),
            AssistantMessage(content="Still prose, I am afraid."),
        )
        assert await scorer.judge([resolved()], LEG) == ()

    async def test_a_well_formed_reply_is_read_without_salvage(self):
        """The fast path stays the path: whole-reply parse first, scan only on failure."""
        scorer, _ = judge(says({"scores": [{"index": 0, "score": 0.8, "reason": "great"}]}))
        assert (await scorer.judge([resolved()], LEG))[0].score == pytest.approx(0.8)

    async def test_a_brace_inside_a_reason_does_not_split_an_entry(self):
        scorer, _ = judge(
            AssistantMessage(
                content=(
                    '{"scores":['
                    '{"index":0,"score":0.9,"reason":"worth the detour {sic}"},'
                    '{"index:1","score":0.5,"reason":"broken"}'
                    "]}"
                )
            )
        )
        scored = await scorer.judge([resolved("A"), resolved("B")], LEG)
        assert [item.reason for item in scored] == ["worth the detour {sic}"]

    async def test_a_truncated_reply_keeps_the_entries_that_arrived(self):
        """A cut-off stream is the same problem: the last object is broken, the rest are not."""
        scorer, _ = judge(
            AssistantMessage(
                content=(
                    '{"scores":['
                    '{"index":0,"score":0.9,"reason":"first"},'
                    '{"index":1,"score":0.8,"reason":"second"},'
                    '{"index":2,"score":0.7,"rea'
                )
            )
        )
        scored = await scorer.judge([resolved("A"), resolved("B"), resolved("C")], LEG)
        assert [item.score for item in scored] == [0.9, 0.8]


class TestAWholeRouteOfCandidates:
    """One call per corridor has a cliff in it, and whole-route search walks off it.

    Measured against the live model at the judge's own 45 s budget: one batch of forty took
    40.8 s, already at the edge, and a real whole-route corridor produced 162. The stage
    failed with `request to OpenAI failed` and the run reported "0 worth showing".

    Batching touches a recorded decision — one call so the model can compare places against
    each other — so it was measured rather than assumed. The same forty places, scored whole
    and scored in twenties:

        one batch of 40 :  40.8s
        two batches of 20:  25.9s
        score delta: median 0.05, max 0.25
        top-3 overlap 3/3, top-5 4/5, top-10 7/10

    Median 0.05 is the run-to-run variance of the *identical* whole batch, measured this
    morning at median 0.05 and max 0.15. Splitting disturbs the ranking about as much as
    asking twice does, and twenty is still a field to compare — the objection in the recorded
    decision is to per-candidate calls, which this is not.
    """

    @staticmethod
    def _batch(size: int):
        return [resolved(f"Place {index}") for index in range(size)]

    @staticmethod
    def _scores(indices):
        return says(
            {"scores": [{"index": index, "score": 0.8, "reason": "worth it"} for index in indices]}
        )

    async def test_a_large_batch_is_split_into_several_calls(self):
        """Each reply scores something, or the retry-on-nothing consumes another call."""
        size = JUDGE_BATCH_SIZE * 2 + 1
        scorer, client = judge(*(self._scores([0]) for _ in range(3)))
        await scorer.judge(self._batch(size), LEG)
        assert len(client.conversations) == 3

    async def test_a_batch_that_fits_is_still_one_call(self):
        scorer, client = judge(self._scores([0]))
        await scorer.judge(self._batch(JUDGE_BATCH_SIZE), LEG)
        assert len(client.conversations) == 1

    async def test_each_batch_is_indexed_from_zero(self):
        """The model answers by position within what it was shown, not within the corridor."""
        scorer, _ = judge(self._scores([0]), self._scores([0]))
        scored = await scorer.judge(self._batch(JUDGE_BATCH_SIZE + 1), LEG)
        assert {item.resolved.candidate.name for item in scored} == {
            "Place 0",
            f"Place {JUDGE_BATCH_SIZE}",
        }

    async def test_the_batch_size_leaves_room_under_the_timeout(self):
        """Forty took 40.8 s against a 45 s budget. Twenty is measured, not interpolated."""
        assert JUDGE_BATCH_SIZE <= 40

    async def test_one_failed_batch_does_not_cost_the_others(self):
        scorer, _ = judge(LlmUnavailable("request to OpenAI failed"), self._scores([0]))
        scored = await scorer.judge(self._batch(JUDGE_BATCH_SIZE + 1), LEG)
        assert [item.resolved.candidate.name for item in scored] == [f"Place {JUDGE_BATCH_SIZE}"]

    async def test_results_are_ranked_across_batches_not_within_them(self):
        """Selection takes the best few overall, so the returned order has to be global."""
        scorer, _ = judge(
            says({"scores": [{"index": 0, "score": 0.2, "reason": "dull"}]}),
            says({"scores": [{"index": 0, "score": 0.9, "reason": "superb"}]}),
        )
        scored = await scorer.judge(self._batch(JUDGE_BATCH_SIZE + 1), LEG)
        assert [item.score for item in scored] == [0.9, 0.2]

    async def test_every_batch_failing_is_still_a_failure(self):
        scorer, _ = judge(
            LlmUnavailable("timed out"),
            LlmUnavailable("timed out"),
            LlmUnavailable("timed out"),
            LlmUnavailable("timed out"),
        )
        with pytest.raises(LlmUnavailable):
            await scorer.judge(self._batch(JUDGE_BATCH_SIZE + 1), LEG)


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


class TestTheFailureLineSaysHowBigTheBatchWas:
    """A batch failing and the corridor collapsing must not read the same.

    One is a batch that went wrong among six that did not; the other is the whole corridor
    collapsing. Since batching landed, the number in the log is the batch size, and a reader
    meeting it a week later has no way to tell which of the two they are looking at unless
    the line says so. This is the third stage in one day whose silent collapse looked like an
    empty corridor, and every hour of that was spent on ambiguity in exactly this place.
    """

    async def test_it_names_the_batch_and_the_corridor(self, caplog):
        scorer, _ = judge(*(says({"scores": []}) for _ in range(8)))
        with caplog.at_level(logging.WARNING, logger=JUDGE_LOGGER):
            await scorer.judge([resolved(f"P{index}") for index in range(30)], LEG)
        assert "20 of 30" in caplog.text

    async def test_a_corridor_that_fits_one_batch_says_so_plainly(self):
        """No "of 5" when the batch is the whole thing; that reads as a partial failure."""
        scorer, _ = judge(says({"scores": []}), says({"scores": []}))
        import logging as _logging

        records: list[str] = []

        class Catch(_logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = _logging.getLogger(JUDGE_LOGGER)
        handler = Catch()
        logger.addHandler(handler)
        try:
            await scorer.judge([resolved(f"P{index}") for index in range(5)], LEG)
        finally:
            logger.removeHandler(handler)
        assert any("all 5" in message for message in records)


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


class TestScoringReportsProgress:
    """ "Scoring 41 places" sat still for half a minute, which is Tim's complaint.

    It cannot be forty-one calls: the judge compares candidates against each other, and
    splitting the batch is what buys the ranking. So the call stays whole and the *reply* is
    streamed, counting entries as they parse — one call, ranking intact, real increments.

    The batch is also four times anything measured before. Road-expansion plus a three-day
    corridor produced 41 candidates where the judge had only ever been exercised at 6 to 11.
    """

    @staticmethod
    def _reply(count: int) -> str:
        entries = ", ".join(
            f'{{"index": {i}, "score": 0.8, "reason": "good"}}' for i in range(count)
        )
        return '{"scores": [' + entries + "]}"

    class Streaming(FakeLlmClient):
        """A model that emits its reply in fragments, as a real one does."""

        def __init__(self, text: str, *, pieces: int = 12):
            super().__init__(replies=(AssistantMessage(content=text),), repeat_last=True)
            size = max(len(text) // pieces, 1)
            self.fragments = [text[i : i + size] for i in range(0, len(text), size)]

        async def stream(self, messages):
            for fragment in self.fragments:
                yield fragment

    async def test_it_reports_each_score_as_it_arrives(self):
        client = self.Streaming(self._reply(5))
        seen: list[int] = []
        scored = await CandidateJudge(client).judge(
            [resolved(f"P{i}") for i in range(5)], LEG, on_progress=lambda n, total: seen.append(n)
        )
        assert len(scored) == 5
        assert seen == [1, 2, 3, 4, 5], "progress should count up, once per score"

    async def test_it_reports_the_total(self):
        client = self.Streaming(self._reply(3))
        totals: list[int] = []
        await CandidateJudge(client).judge(
            [resolved(f"P{i}") for i in range(3)],
            LEG,
            on_progress=lambda n, total: totals.append(total),
        )
        assert totals == [3, 3, 3], "the denominator is how many were sent, known up front"

    async def test_the_scores_are_the_same_as_unstreamed(self):
        """Streaming is a reporting change. It must not alter the ranking, which is the
        thing the single call was protecting."""
        client = self.Streaming(self._reply(4))
        scored = await CandidateJudge(client).judge([resolved(f"P{i}") for i in range(4)], LEG)
        assert [item.score for item in scored] == [0.8] * 4

    async def test_no_callback_still_works(self):
        client = self.Streaming(self._reply(2))
        assert len(await CandidateJudge(client).judge([resolved("A"), resolved("B")], LEG)) == 2

    async def test_a_reply_split_mid_entry_still_parses(self):
        """Fragments do not respect JSON boundaries; a score arrives across two chunks."""
        client = self.Streaming(self._reply(6), pieces=40)
        scored = await CandidateJudge(client).judge([resolved(f"P{i}") for i in range(6)], LEG)
        assert len(scored) == 6

    async def test_an_unusable_stream_is_still_retried(self):
        """The retry is not lost by streaming: prose twice still ends empty, not raising."""
        client = self.Streaming("I have thoughts but no JSON.")
        assert await CandidateJudge(client).judge([resolved("A")], LEG) == ()
