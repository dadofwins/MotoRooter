"""Reading place names out of search results.

The stage the Chinook Pass spike proved was missing. Search returns pages *about* places —
directory listings, magazine articles, reddit threads — and the place name is usually in the
prose rather than the title:

    title:   Dispersed Camping near Chinook, WA                    (a directory)
    snippet: ...the most popular is Road to Snag Lake - Dispersed...

Resolving that title against Places yields nothing. Resolving "Road to Snag Lake" yields a
place. Pulling the second out of the first is a language task, which makes it the right thing
to ask a model for, and a much narrower question than the scoring one.

Two properties keep it honest.

**Every name is checked against its source.** The model is told it may only name places that
appear in the text — and then the output is checked, because a prompt instruction is not a
guard. See `grounding`. The cost is strict: a genuine place the model reworded gets dropped.
The alternative is putting a campsite that does not exist in front of someone at dusk.

**One call for the whole batch.** Extraction must not become a fan-out multiplier — anchors
times categories is already the metered number that matters.

Nothing here raises on a bad reply. A model that answers with prose, or invents everything,
should cost candidates rather than the corridor run that has already paid for its searches.
"""

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from motorooter.llm.messages import Message, SystemMessage, UserMessage
from motorooter.llm.protocol import LlmClient
from motorooter.planning.discovery.grounding import appears_in, normalize
from motorooter.planning.discovery.models import Candidate

logger = logging.getLogger(__name__)

DEFAULT_MAX_PLACES_PER_RESULT = 3
"""Places accepted from one search result.

The first live run turned a single snippet into eight names — the campground, the town, the
national forest, the national park and the pass. Most were context rather than destinations,
and every survivor costs a Places lookup downstream."""

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
"""Models wrap JSON in code fences and apologies. Losing every candidate to a ``` would be
a silly way to fail, so the outermost object is extracted rather than the reply parsed whole."""

SYSTEM_PROMPT = """\
You read web search results and identify the specific, visitable places they describe.

A result is often a directory page, a magazine article or a forum thread rather than a place.
Your job is to name the actual places mentioned in its title or text — a campground, a
viewpoint, a diner — not to describe the page.

Rules:
- Only name a place whose name appears, word for word, in the text you were given. Never
  infer, complete or invent a name. If a result names no specific place, return none for it.
- Name the specific place someone would stop at, never the geography containing it. Do not
  name states, counties, cities, towns, national forests, national parks, mountain ranges or
  the search area itself. "Road to Snag Lake" yes; "Okanogan-Wenatchee National Forest" no.
- Mark a result irrelevant if it is about a different region from the one being searched.
- Return only JSON, in this shape:

{"places": [{"result_index": 0, "place_name": "...", "relevant": true}]}
"""


class PlaceExtractor:
    """Turns search results into candidates named after places rather than pages."""

    def __init__(self, client: LlmClient) -> None:
        self._client = client

    async def extract(
        self,
        results: Sequence[Candidate],
        *,
        region: str | None = None,
        searched_for: str | None = None,
        max_per_result: int = DEFAULT_MAX_PLACES_PER_RESULT,
    ) -> tuple[Candidate, ...]:
        """Named places from a batch of search results.

        Args:
            results: what search returned, whose `name` is a page title.
            region: disambiguation for ambiguous names — "Cayuse" matched Oregon on a
                Washington corridor. Offered to the model and stripped before grounding, so
                our own qualifier cannot fail its own check.
            searched_for: the place the query was about. Dropped from the results, because
                the corridor anchor is context rather than a discovery — and dropped here
                rather than asked for in the prompt, since we know what we searched.
            max_per_result: ceiling per source. Each survivor costs a Places lookup, so the
                model does not get to set that budget.

        Returns whatever survived. Never raises on a bad reply.
        """
        if not results:
            return ()

        reply = await self._client.complete(self._conversation(results, region), [])
        extracted = _places_in(reply.content)

        candidates: list[Candidate] = []
        seen: set[str] = set()
        per_result: dict[int, int] = {}
        excluded = normalize(searched_for) if searched_for else None

        for entry in extracted:
            candidate = self._accept(entry, results, region)
            if candidate is None:
                continue

            key = normalize(candidate.name)
            if key == excluded:
                # The place we searched for. Real, grounded, and not a discovery.
                continue
            if key in seen:
                # Two results describing one campground is the normal case.
                continue

            index = int(entry["result_index"])
            if per_result.get(index, 0) >= max_per_result:
                continue

            seen.add(key)
            per_result[index] = per_result.get(index, 0) + 1
            candidates.append(candidate)
        return tuple(candidates)

    def _conversation(self, results: Sequence[Candidate], region: str | None) -> list[Message]:
        lines = []
        if region:
            lines.append(f"The corridor being searched is in {region}.")
            lines.append(
                f"If a place name is ambiguous, qualify it as '<name>, {region}'. "
                "Do not qualify names that are already unambiguous."
            )
        lines.append("Results:")
        for index, result in enumerate(results):
            lines.append(f"[{index}] {result.name}")
            if result.snippet:
                lines.append(f"    {result.snippet}")
        return [SystemMessage(content=SYSTEM_PROMPT), UserMessage(content="\n".join(lines))]

    @staticmethod
    def _accept(
        entry: dict[str, Any], results: Sequence[Candidate], region: str | None
    ) -> Candidate | None:
        index = entry.get("result_index")
        name = entry.get("place_name")
        if not isinstance(index, int) or not isinstance(name, str):
            return None
        # Bounds checked explicitly: Python would read -1 from the end and ground the name
        # against a different result than the one it claims to have come from.
        if not 0 <= index < len(results):
            return None
        if entry.get("relevant", True) is False:
            return None

        source = results[index]
        # Title and snippet together: the place is often only in the title.
        text = f"{source.name}\n{source.snippet or ''}"
        if not appears_in(name, text, region=region):
            logger.info("dropped extracted place %r: not present in the result it came from", name)
            return None

        return source.model_copy(update={"name": name.strip()})


def _places_in(content: str | None) -> list[dict[str, Any]]:
    """The `places` list from a model reply, or none.

    Every failure mode returns empty rather than raising. A model answering with an apology
    is an ordinary event, and it should cost the candidates from one batch — not a corridor
    run that has already spent its search budget.
    """
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
    places = body.get("places")
    if not isinstance(places, list):
        return []
    return [entry for entry in places if isinstance(entry, dict)]
