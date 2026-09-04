"""Turning measured facts into one line of copy.

Everything that can go wrong here produces no blurb rather than an error. The header is
decoration: the rail keeps its static line, the rider notices nothing, and nobody is shown a
stack trace because a piece of flavour text did not arrive.
"""

import logging
from collections.abc import Sequence

from motorooter.blurb.facts import TripFacts, facts_for
from motorooter.blurb.models import Turn
from motorooter.blurb.prompt import BLURB_SYSTEM_PROMPT
from motorooter.llm.errors import LlmError
from motorooter.llm.messages import Message, SystemMessage, UserMessage
from motorooter.llm.protocol import LlmClient
from motorooter.trips.models import Trip

logger = logging.getLogger(__name__)

MAX_BLURB_CHARS = 160
"""Longest line accepted, beyond which there is no blurb at all.

The rail is 380px. A line over this either wraps to three rows and pushes the conversation
down, or gets cut off mid-word — and a severed sentence reads as a bug, while the static
header reads as normal. Dropping it is the better of the two failures, and the prompt asks
for about a dozen words, so hitting this at all means the model ignored the brief.
"""

MAX_HISTORY_TURNS = 6
"""Recent turns passed as context. The blurb is about the trip; history only colours it."""


class BlurbWriter:
    """Writes one line about a trip, or returns `None` and lets the header stay as it was."""

    def __init__(self, model: LlmClient) -> None:
        self._model = model

    async def write(self, trip: Trip, history: Sequence[Turn] = ()) -> str | None:
        """One line, or `None` if there is not one worth showing."""
        facts = facts_for(trip)
        messages: list[Message] = [
            SystemMessage(content=BLURB_SYSTEM_PROMPT),
            UserMessage(content=_evidence(facts, history)),
        ]
        try:
            reply = await self._model.complete(messages, tools=())
        except LlmError:
            # Decoration. A failure here costs a line of flavour text, so it is logged for
            # the operator at info and never raised at the rider.
            logger.info("no blurb written for %r", trip.slug, exc_info=True)
            return None
        return _one_line(reply.content)


def _one_line(content: str | None) -> str | None:
    """The reply as a single short line, or `None` if it is not usable as one."""
    if not content:
        return None
    line = " ".join(content.split())
    if not line or len(line) > MAX_BLURB_CHARS:
        return None
    return line


def _evidence(facts: TripFacts, history: Sequence[Turn]) -> str:
    """Everything the model is allowed to draw on, and nothing else.

    Built from `TripFacts` alone. That is what makes the prompt's "state nothing you were not
    given" checkable rather than aspirational — there is no path by which a figure reaches
    the model without passing through a measurement.
    """
    lines = ["Notes on this trip:"]

    if not facts.waypoint_names and not facts.leg_count:
        lines.append("- nothing on the map yet; the rider has not placed a point")
    else:
        shape = "a loop" if facts.is_loop else "a point-to-point run"
        lines.append(f"- shape: {shape}, {facts.leg_count} leg(s)")
        if facts.waypoint_names:
            lines.append(f"- stops, in order: {', '.join(facts.waypoint_names)}")

    if facts.distance_km is not None:
        lines.append(f"- distance: {facts.distance_km:.0f} km")
    else:
        lines.append("- distance: not routed yet, so unknown")

    if facts.unpaved_share is not None:
        lines.append(
            f"- surface: {facts.unpaved_share:.0%} unpaved, "
            f"{(facts.paved_share or 0.0):.0%} paved, "
            f"{(facts.unsurveyed_share or 0.0):.0%} unsurveyed"
        )

    if facts.riding_modes:
        lines.append(f"- riding mode: {', '.join(facts.riding_modes)}")

    if facts.place_counts:
        found = ", ".join(f"{count} {kind}" for kind, count in sorted(facts.place_counts.items()))
        lines.append(f"- places saved: {found}")
        # Name and category together, never two lists to be joined by guesswork.
        named = ", ".join(f"{name} ({kind})" for name, kind in facts.named_places)
        lines.append(f"- some of them: {named}")
    else:
        lines.append("- places saved: none yet")

    if history:
        lines.append("")
        lines.append("Recently said in the rail, oldest first:")
        lines += [f"  {turn.role}: {turn.content}" for turn in history[-MAX_HISTORY_TURNS:]]

    return "\n".join(lines)
