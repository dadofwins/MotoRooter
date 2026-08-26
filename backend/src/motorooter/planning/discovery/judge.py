"""Scoring a resolved place, given evidence it did not have to guess at.

The last stage, and structurally the easiest one to get quietly wrong. Everything measurable
has already been measured — twistiness, surface, detour, remoteness, the Places rating — and
is handed over as fact. What is left is the part that genuinely needs judgement: is this
scenic, is it locally known, is the detour worth it, does the ride report say the road washes
out in spring.

**The model returns a score and a sentence. Nothing else.** Not a coordinate, not a name, not
a distance — those already exist and are correct, and letting a model restate them is how a
correct pin acquires a wrong location two stages after anyone was looking. Invention is
structurally impossible here rather than discouraged: only `score` and `reason` are read from
the reply, and everything else on the result comes from the `ResolvedCandidate` it was built
from.

**Resist adding instructions to the prompt.** If a truck stop outranks a mountain viewpoint,
the fix is almost always a metric the model was not given rather than a sentence telling it
to prefer viewpoints. A preference encoded in a prompt is invisible to tests; a metric is
not.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence

from motorooter.llm.errors import LlmError
from motorooter.llm.messages import Message, SystemMessage, UserMessage
from motorooter.llm.protocol import LlmClient
from motorooter.planning.discovery.corridor import SearchCorridor
from motorooter.planning.discovery.evidence import assemble
from motorooter.planning.discovery.models import Evidence, ResolvedCandidate, ScoredCandidate

logger = logging.getLogger(__name__)

JUDGE_BATCH_SIZE = 20
"""How many places to score in one call.

The stage had no ceiling, and whole-route search walks off the cliff that leaves: a real
corridor produced 162 candidates and the request failed outright, so the run reported "0 worth
showing" — the same summary an empty corridor gives.

Batching touches a decision recorded in CLAUDE.md, so it was measured rather than assumed. The
same forty places, judged whole and judged in twenties:

    one batch of 40 :  40.8s     (the budget is 45 s, so forty is already the edge)
    two batches of 20:  25.9s
    score delta: median 0.05, max 0.25
    top-3 overlap 3/3, top-5 4/5, top-10 7/10

Median 0.05 is the run-to-run variance of the *identical* whole batch, measured at median 0.05
and max 0.15. Splitting disturbs the ranking about as much as asking the same question twice
does — and the whole selection layer is built on the score being a ranking rather than a
measurement, for exactly that reason.

What the recorded decision objects to is a call *per candidate*. Twenty is still a field to
compare, and the batches run concurrently, so forty candidates got faster rather than slower.

Twenty rather than a rounder thirty because twenty is what was measured for both latency and
agreement.
"""

MAX_LOGGED_REPLY_CHARS = 2000
"""How much of an unusable reply to record.

Enough to see whether the model answered with prose, truncated JSON, or nothing at all —
which is the distinction that would explain this — and not so much that a model answering
with an essay fills the log with it.
"""

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = """\
You score places a motorcycle rider might stop at, along a route they are planning.

You are given measurements that are already correct — distance off the route, how twisty and
what surface the nearby road is, how far the nearest fuel is, and the place's own rating.
Trust them. Do not restate them, recompute them, or contradict them.

Judge only what the measurements cannot say: whether the place is worth the detour for
someone on a motorcycle. A memorable viewpoint beats a chain restaurant. Local knowledge in
the description — that a road washes out, that a diner is a rider institution — matters more
than a generic listing.

Return only JSON:

{"scores": [{"index": 0, "score": 0.0, "reason": "one short sentence"}]}

score is 0 to 1. reason is one sentence, in your own words, saying why.

Do not put numeric ratings or quoted review text in the reason. Say "well-rated", not "4.6
from 59,117 ratings", and describe what reviewers convey rather than quoting them.
"""
"""The reason is kept on the trip, so it has to be ours rather than Google's.

The rating is deliberately *given* to the judge — it is a fact about the place and the model
should not be made to guess it — so the terms boundary cannot sit on the prompt's input. It
sits on what comes back: a characterisation we wrote is ours to store, a Places field with a
sentence around it is a cache with extra steps. "Well-rated" is more use to a rider than
"4.6" in any case.
"""


class CandidateJudge:
    """Scores resolved candidates against measured evidence."""

    def __init__(self, client: LlmClient) -> None:
        self._client = client

    async def judge(
        self,
        resolved: Sequence[ResolvedCandidate],
        leg: SearchCorridor,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[ScoredCandidate, ...]:
        """Score every candidate, best first, in batches small enough to come back.

        Not one call per candidate: scoring must not become a fan-out multiplier, and a model
        comparing places against each other ranks them better than one seeing each alone. That
        is the decision this keeps. What it drops is *one call for the whole corridor*, which
        had a cliff in it — see `JUDGE_BATCH_SIZE`.

        Ranked across batches rather than within them, because selection takes the best few
        overall and never looks below that.

        Raises:
            LlmError: every batch failed. A batch that fails alone costs its own scores, the
                way a failed categorise batch does; all of them failing has to be audible, or
                a corridor that could not be judged looks like a corridor with nothing in it.
        """
        if not resolved:
            return ()

        batches = [
            resolved[start : start + JUDGE_BATCH_SIZE]
            for start in range(0, len(resolved), JUDGE_BATCH_SIZE)
        ]
        settled = await asyncio.gather(
            *(self._judge_batch(batch, leg, on_progress) for batch in batches),
            return_exceptions=True,
        )

        scored: list[ScoredCandidate] = []
        failures: list[LlmError] = []
        for batch, outcome in zip(batches, settled, strict=True):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, LlmError):
                    raise outcome
                logger.warning("judging %d places failed: %s", len(batch), outcome)
                failures.append(outcome)
                continue
            scored.extend(outcome)

        if failures and not scored:
            raise failures[0]
        return tuple(sorted(scored, key=lambda item: item.score, reverse=True))

    async def _judge_batch(
        self,
        resolved: Sequence[ResolvedCandidate],
        leg: SearchCorridor,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[ScoredCandidate, ...]:
        """One call, retried once if it yields nothing usable.

        Never raises on a bad *reply*. A model answering with prose should cost the scores
        from one batch, not a corridor run that has already paid for its searches and lookups.
        A failed *request* does raise, so the caller can tell one batch's silence from the
        model being unreachable.
        """
        if not resolved:
            return ()

        evidence = [assemble(candidate, leg, others=resolved) for candidate in resolved]
        conversation = self._conversation(resolved, evidence)

        # Asked twice if the first reply yields nothing usable. Measured across four live
        # runs of one corridor, three produced zero scores from five to eight resolved
        # candidates — after every search, extraction and Places lookup had been paid for.
        #
        # It is not batch size (twenty score fine) and not the timeout (the failing runs
        # finished well inside it), and it has resisted reproduction. Surviving it is worth
        # more than explaining it: this is one call, it works most of the time, and a second
        # attempt costs one request against a corridor's worth of work already spent.
        #
        # Only on *nothing*. A partial answer is a judgement — the model declining to score
        # one place — and asking again would discard the scores it did give.
        for attempt in (1, 2):
            content = await self._read(conversation, len(resolved), on_progress)
            scored = self._parse(content, resolved, evidence)
            if scored:
                return scored
            # Recorded every time, not only when someone is watching. This has resisted
            # reproduction, so waiting to catch one is the expensive order — logging it
            # means the next occurrence diagnoses itself. Server-side only: the raw reply
            # is logged here and never travels back to the model or to a client.
            logger.warning(
                "judge scored none of %d places on attempt %d; reply was: %.*s",
                len(resolved),
                attempt,
                MAX_LOGGED_REPLY_CHARS,
                content or "<empty>",
            )
            if attempt == 2:
                return scored
        raise AssertionError("unreachable: the loop returns on both attempts")  # pragma: no cover

    async def _read(
        self,
        conversation: list[Message],
        total: int,
        on_progress: Callable[[int, int], None] | None,
    ) -> str | None:
        """The whole reply, reporting each score as it lands.

        Streamed rather than awaited so the count can move. The call is not split — the judge
        ranks candidates against each other and splitting the batch is what would cost the
        ranking — so the increments come from the reply arriving, not from the work being
        divided.

        Falls back to a plain completion when the client cannot stream, which keeps every
        `LlmClient` usable here and means a fake in a test need not implement both.
        """
        streamer = getattr(self._client, "stream", None)
        if streamer is None:
            reply = await self._client.complete(conversation, [])
            return reply.content

        pieces: list[str] = []
        counted = 0
        async for piece in streamer(conversation):
            pieces.append(piece)
            if on_progress is None:
                continue
            # Counting closing braces of score objects is enough to know one arrived, and it
            # does not require the accumulated text to be valid JSON yet — which, mid-stream,
            # it never is.
            seen = min(_completed_entries("".join(pieces)), total)
            while counted < seen:
                counted += 1
                on_progress(counted, total)
        return "".join(pieces)

    def _parse(
        self,
        content: str | None,
        resolved: Sequence[ResolvedCandidate],
        evidence: Sequence[Evidence],
    ) -> tuple[ScoredCandidate, ...]:
        """Usable scores from one reply, best first. Anything unusable is dropped."""
        scored: list[ScoredCandidate] = []
        for entry in _scores_in(content):
            index = entry.get("index")
            if not isinstance(index, int) or not 0 <= index < len(resolved):
                continue
            score = _bounded(entry.get("score"))
            reason = entry.get("reason")
            if score is None or not isinstance(reason, str) or not reason.strip():
                # A score with no reason is unreviewable, which defeats the point of the
                # stage: the first question anyone asks is why a road they love scored 0.3.
                continue
            scored.append(
                ScoredCandidate(
                    resolved=resolved[index],
                    evidence=evidence[index],
                    score=score,
                    reason=reason.strip(),
                )
            )
        return tuple(sorted(scored, key=lambda item: item.score, reverse=True))

    @staticmethod
    def _conversation(
        resolved: Sequence[ResolvedCandidate], evidence: Sequence[Evidence]
    ) -> list[Message]:
        lines = ["Places to score:"]
        for index, (candidate, facts) in enumerate(zip(resolved, evidence, strict=True)):
            lines.append(
                f"[{index}] {candidate.candidate.name} ({candidate.candidate.category.value})"
            )
            lines.append(f"    measured: {_describe(facts)}")
            if candidate.candidate.found_via:
                # The reason expansion is worth its extra searches: the same viewpoint is
                # worth more on a road people ride for pleasure than on one nobody mentions.
                lines.append(f"    found on: {candidate.candidate.found_via}")
            if candidate.candidate.snippet:
                lines.append(f"    said of it: {candidate.candidate.snippet}")
        return [SystemMessage(content=SYSTEM_PROMPT), UserMessage(content="\n".join(lines))]


def _describe(facts: Evidence) -> str:
    """Measurements as prose, omitting what was not measured.

    Absent signals are left out rather than rendered as "unknown" or zero: a model shown
    "unpaved 0%" for a road nobody surveyed will reason about tarmac that may not be there.
    """
    parts: list[str] = []
    if facts.distance_off_route_m is not None:
        parts.append(f"{facts.distance_off_route_m:.0f} m off route")
    if facts.twistiness_deg_per_km is not None:
        parts.append(f"nearby road {facts.twistiness_deg_per_km:.0f} deg/km of turn")
    # Either a surface reading or an admission that there is not one — never both. A road
    # nobody surveyed reports `unpaved_fraction` of 0.0, which is arithmetically true and
    # reads as "this is not a dirt road". It is not the same claim.
    unsurveyed = (facts.unknown_surface_fraction or 0.0) > 0.5
    if unsurveyed:
        parts.append("surface not recorded for this stretch")
    elif facts.unpaved_fraction is not None:
        parts.append(f"{facts.unpaved_fraction:.0%} unpaved nearby")
    if facts.distance_to_fuel_m is not None:
        parts.append(f"nearest known fuel {facts.distance_to_fuel_m / 1000:.1f} km")
    if facts.rating is not None:
        count = f" from {facts.user_rating_count}" if facts.user_rating_count else ""
        parts.append(f"rated {facts.rating:.1f}{count}")
    return "; ".join(parts) if parts else "nothing measured"


def _bounded(value: object) -> float | None:
    """A score in [0, 1], or nothing.

    Out of range is rejected rather than clamped: a model returning 11 has misunderstood the
    scale, and clamping to 1.0 makes that indistinguishable from a considered top mark.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def _scores_in(content: str | None) -> list[dict[str, object]]:
    """Score objects from a reply, whether or not the reply as a whole is valid JSON."""
    if not content:
        return []
    match = _JSON_OBJECT.search(content)
    if match is not None:
        try:
            body = json.loads(match.group())
        except ValueError:
            pass
        else:
            if isinstance(body, dict) and isinstance(body.get("scores"), list):
                return [entry for entry in body["scores"] if isinstance(entry, dict)]
    return _salvaged(content)


def _salvaged(content: str) -> list[dict[str, object]]:
    """Whatever score objects can still be read out of a reply that will not parse.

    The judge-zero cause, and it cost whole batches. Two live captures, both a quote in the
    wrong place in a key:

        {"index:3","score":0.50,"reason":"..."}
        {"index:2,"score":0.7,"reason":"..."}

    One of those makes `json.loads` fail on the *entire* reply, so twenty perfectly good
    scores were discarded and the batch asked again. The retry is why this only ever showed
    up as slowness rather than as an error.

    `raw_decode` from every `{` rather than a brace-counting scan, because the second capture
    has an *odd* number of quotes: any parser tracking string state itself is left inside a
    string for the rest of the reply and loses every later entry too. Looking for a literal
    brace and asking the real decoder whether a value starts there has no state to corrupt —
    a `{` inside prose, or inside a reason, simply fails to decode and is stepped over.

    Nothing is repaired. The damaged field in both captures is the index, which is the only
    thing tying a score to a place, so a guess would attach a judgement to a different
    campsite — the plausible-and-wrong failure every other stage of discovery refuses.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, object]] = []
    position = 0
    while (start := content.find("{", position)) != -1:
        try:
            value, end = decoder.raw_decode(content, start)
        except ValueError:
            position = start + 1
            continue
        position = end
        if isinstance(value, dict):
            # Not filtered to things that look like scores: `_parse` already requires a
            # usable index, score and reason, so a stray object is dropped one step later
            # and a second guard here would be a condition no test could reach.
            found.append(value)
    return found


def _completed_entries(partial: str) -> int:
    """How many score objects have finished arriving in a partial reply.

    Counts closing braces at object depth two — inside the top-level object, inside the
    `scores` array — which is where a score sits. Brace counting rather than parsing because
    a partial reply is not valid JSON at any point before the last character, and the whole
    purpose is to say something before then.

    Strings are skipped so a brace inside a reason does not count, which a model writing
    "worth the detour {sic}" would otherwise trigger.
    """
    depth = 0
    completed = 0
    in_string = False
    escaped = False
    for character in partial:
        if escaped:
            escaped = False
            continue
        if character == "\\" and in_string:
            escaped = True
            continue
        if character == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            if depth == 2:
                completed += 1
            depth -= 1
    return completed
