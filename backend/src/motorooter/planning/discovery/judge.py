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

import json
import logging
import re
from collections.abc import Sequence

from motorooter.llm.messages import Message, SystemMessage, UserMessage
from motorooter.llm.protocol import LlmClient
from motorooter.planning.discovery.evidence import assemble
from motorooter.planning.discovery.models import Evidence, ResolvedCandidate, ScoredCandidate
from motorooter.routing.models import RouteLeg

logger = logging.getLogger(__name__)

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
"""


class CandidateJudge:
    """Scores resolved candidates against measured evidence."""

    def __init__(self, client: LlmClient) -> None:
        self._client = client

    async def judge(
        self, resolved: Sequence[ResolvedCandidate], leg: RouteLeg
    ) -> tuple[ScoredCandidate, ...]:
        """Score a batch, dropping anything the model did not usably score.

        One call for the batch rather than per candidate: scoring must not become a fan-out
        multiplier, and a model comparing places against each other ranks them better than
        one seeing each alone.

        Never raises on a bad reply. A model answering with prose should cost the scores from
        one batch, not a corridor run that has already paid for its searches and lookups.
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
            reply = await self._client.complete(conversation, [])
            scored = self._parse(reply.content, resolved, evidence)
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
                reply.content or "<empty>",
            )
            if attempt == 2:
                return scored
        raise AssertionError("unreachable: the loop returns on both attempts")  # pragma: no cover

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
    if not content:
        return []
    match = _JSON_OBJECT.search(content)
    if match is None:
        return []
    try:
        body = json.loads(match.group())
    except ValueError:
        return []
    if not isinstance(body, dict):
        return []
    scores = body.get("scores")
    if not isinstance(scores, list):
        return []
    return [entry for entry in scores if isinstance(entry, dict)]
