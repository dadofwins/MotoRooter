"""Deciding what kind of place something is, from what Places says it is.

The category used to be inherited from the query that found it — which is how a ski resort
turned up tagged `wild_camp`, having appeared in a dispersed-camping search. Systematic
rather than unlucky: every query mislabels whatever it surfaces that is not what it asked for.

Places' own `types` are deterministic and arrive free with a lookup already being made, so
they decide wherever they can. The model is asked only where Places is silent or cannot draw
a distinction a rider cares about.

The query's category is not a fallback. A plausible-looking wrong category is worse than an
absent one — it puts the wrong icon on the map and gets filtered into the wrong list, and
nothing about it looks broken.
"""

import json
import re
from collections.abc import Iterable, Sequence
from types import MappingProxyType

from motorooter.llm.messages import Message, SystemMessage, UserMessage
from motorooter.llm.protocol import LlmClient
from motorooter.planning.discovery.models import ResolvedCandidate
from motorooter.trips.models import PoiCategory

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

PLACES_TYPE_TO_CATEGORY: MappingProxyType[str, PoiCategory] = MappingProxyType(
    {
        "campground": PoiCategory.CAMPGROUND,
        "rv_park": PoiCategory.CAMPGROUND,
        "camping_cabin": PoiCategory.CAMPGROUND,
        "lodging": PoiCategory.HOTEL,
        "hotel": PoiCategory.HOTEL,
        "motel": PoiCategory.HOTEL,
        "inn": PoiCategory.HOTEL,
        "bed_and_breakfast": PoiCategory.HOTEL,
        "guest_house": PoiCategory.HOTEL,
        "resort_hotel": PoiCategory.HOTEL,
        "restaurant": PoiCategory.FOOD,
        "cafe": PoiCategory.FOOD,
        "diner": PoiCategory.FOOD,
        "bar": PoiCategory.FOOD,
        "bakery": PoiCategory.FOOD,
        "meal_takeaway": PoiCategory.FOOD,
        "gas_station": PoiCategory.FUEL,
        "electric_vehicle_charging_station": PoiCategory.FUEL,
        "tourist_attraction": PoiCategory.VIEWPOINT,
        "scenic_point": PoiCategory.VIEWPOINT,
        "observation_deck": PoiCategory.VIEWPOINT,
        "national_park": PoiCategory.VIEWPOINT,
        "state_park": PoiCategory.VIEWPOINT,
        "park": PoiCategory.VIEWPOINT,
        "hiking_area": PoiCategory.VIEWPOINT,
        "car_repair": PoiCategory.MECHANIC,
        "motorcycle_repair": PoiCategory.MECHANIC,
    }
)
"""Places type to our category.

`WILD_CAMP` is deliberately absent: Google has no dispersed-camping type, and mapping
something to it would invent a distinction Places does not draw. That gap is precisely what
the model is asked about.

`point_of_interest` and `establishment` are absent too. They sit on almost everything, so
mapping them would make this branch always fire and the model never run.
"""


NOT_A_PLACE_TYPES: frozenset[str] = frozenset(
    {"route", "street_address", "premise", "intersection"}
)
"""Places types that describe something you ride through rather than stop at.

`route` is the important one. Places types a highway, a byway and a forest road all as
`route`, and asking the model to categorise one produces a plausible answer — `Suntop Trail`
and `Mather Memorial Highway` both came back as viewpoints on a live run, and would have been
pinned as places a rider could stop at.

`intersection` is the same mistake in miniature: `Sunset Way & 6th Ave NE, Issaquah` arrived
on three of four live runs, resolved, was scored, and was then discarded for having no
category — a metered lookup and a scoring slot spent on a road junction. Refusing it here
costs both, and stops the model being asked a question it will answer rather than decline.

Excluded before the model is asked, rather than left for it to decline. It is the same
roads-are-leads rule the extract stage applies, enforced here with Places' own answer instead
of a judgement.
"""


def is_a_place(types: Iterable[object]) -> bool:
    """Whether Places describes this as somewhere you can stop.

    A road is not, however interesting it is. Following it is the expansion stage's job.
    """
    return not any(isinstance(entry, str) and entry in NOT_A_PLACE_TYPES for entry in types)


def from_places_types(types: Iterable[object]) -> PoiCategory | None:
    """The category Places implies, or `None` if it does not imply one.

    Takes the first recognised type: Places lists them roughly most-specific first, so a
    campground that is also in a park should read as a campground.
    """
    if not is_a_place(types):
        return None
    for entry in types:
        if isinstance(entry, str):
            category = PLACES_TYPE_TO_CATEGORY.get(entry)
            if category is not None:
                return category
    return None


CLASSIFY_PROMPT = """\
You decide what kind of place something is, for a motorcycle trip planner.

You are shown places that Google Places could not categorise usefully, along with whatever
type labels it did return and a description from the web. Pick the single best category.

The distinction that matters most and that Google does not draw: dispersed or informal
camping on public land is `wild_camp`; a campground you can book with facilities is
`campground`. A rider needs to know which before they arrive.

Categories: {categories}

If none fits, say null rather than choosing the nearest one. A wrong category puts the wrong
icon on a map and filters it into the wrong list, and nothing about it looks broken.

Return only JSON: {{"categories": [{{"index": 0, "category": "wild_camp"}}]}}
"""


class CategoryClassifier:
    """Asks the model only about places Places could not type.

    Batched and conditional: most places never reach here, because a deterministic type
    lookup already answered. This is the residue — chiefly dispersed camping, which has no
    Google type at all, and which is one of the things riders most want to find.
    """

    def __init__(self, client: LlmClient) -> None:
        self._client = client

    async def classify(
        self, resolved: Sequence[ResolvedCandidate]
    ) -> tuple[ResolvedCandidate, ...]:
        """Fill in missing categories. Anything still unknown is returned unchanged."""
        # A road is never a place, so the model is not asked about one. Left to it, it
        # answers plausibly — two highways came back as viewpoints on a live run.
        pending = [
            (index, candidate)
            for index, candidate in enumerate(resolved)
            if candidate.category is None and is_a_place(candidate.places_types)
        ]
        if not pending:
            return tuple(resolved)

        reply = await self._client.complete(self._conversation(pending), [])
        assigned = _assignments_in(reply.content)

        updated = list(resolved)
        for position, category in assigned.items():
            if 0 <= position < len(pending):
                index, candidate = pending[position]
                updated[index] = candidate.model_copy(update={"category": category})
        return tuple(updated)

    @staticmethod
    def _conversation(
        pending: Sequence[tuple[int, ResolvedCandidate]],
    ) -> list[Message]:
        lines = ["Places to categorise:"]
        for position, (_, candidate) in enumerate(pending):
            lines.append(f"[{position}] {candidate.candidate.name}")
            if candidate.places_types:
                lines.append(f"    Google types: {', '.join(candidate.places_types)}")
            if candidate.candidate.snippet:
                lines.append(f"    described as: {candidate.candidate.snippet}")
        return [
            SystemMessage(
                content=CLASSIFY_PROMPT.format(
                    categories=", ".join(category.value for category in PoiCategory)
                )
            ),
            UserMessage(content="\n".join(lines)),
        ]


def _assignments_in(content: str | None) -> dict[int, PoiCategory]:
    """Index to category, dropping anything unusable.

    A category the model invented is discarded rather than coerced: the whole point of this
    stage is that an absent category beats a plausible wrong one.
    """
    if not content:
        return {}
    match = _JSON_OBJECT.search(content)
    if match is None:
        return {}
    try:
        body = json.loads(match.group())
    except ValueError:
        return {}
    if not isinstance(body, dict):
        return {}

    assignments: dict[int, PoiCategory] = {}
    for entry in body.get("categories") or []:
        if not isinstance(entry, dict):
            continue
        index, raw = entry.get("index"), entry.get("category")
        if not isinstance(index, int) or not isinstance(raw, str):
            continue
        try:
            assignments[index] = PoiCategory(raw)
        except ValueError:
            continue
    return assignments
