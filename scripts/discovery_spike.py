"""What does discovery actually find along one corridor?

The M0 routing spike worked because it produced something a rider could open and judge in a
minute. This is the same idea for the half of the product that has no reference to check
against: there is no WABDR track that says "yes, that is a good place to camp". Tim is the
reference, so the output has to be something he can read and say "no, I would not stop
there" — before any of it is wired into scoring.

All four stages: search, extract, resolve, judge. Ranked output, with the evidence each
score was based on, so a wrong ranking can be traced to a missing measurement rather than
argued about.

The extract stage exists because the first run of this script showed search returning pages
*about* places rather than places — the output below is the check on whether that is fixed.

    uv run --project backend python scripts/discovery_spike.py

Requires BRAVE_SEARCH_API_KEY, ORS_API_KEY and OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "backend/src"))

from motorooter.planning.discovery.corridor import anchors, spacing_of  # noqa: E402
from motorooter.llm.providers.openai import OpenAiClient  # noqa: E402
from motorooter.planning.discovery.errors import DiscoveryError  # noqa: E402
from motorooter.planning.discovery.extract import PlaceExtractor  # noqa: E402
from motorooter.planning.discovery.category import CategoryClassifier  # noqa: E402
from motorooter.planning.discovery.judge import CandidateJudge, _describe  # noqa: E402
from motorooter.planning.discovery.resolve import PlacesResolver  # noqa: E402
from motorooter.planning.discovery.queries import queries_for, total_queries  # noqa: E402
from motorooter.planning.discovery.sources.brave import BraveSearchSource  # noqa: E402
from motorooter.routing.models import Coordinate, LegIntent, RouteRequest  # noqa: E402
from motorooter.routing.providers.ors import OrsProvider  # noqa: E402
from motorooter.trips.models import PoiCategory  # noqa: E402

# A short, real, interesting section: Chinook Pass. Twisty, paved, and the sort of road
# where the difference between a good and a bad recommendation is obvious to a rider.
START = Coordinate(lat=46.9720, lon=-121.5340)
END = Coordinate(lat=46.8722, lon=-121.5165)

# Hand-supplied, because turning an anchor coordinate into a place name is not built yet —
# Brave cannot search "47.0,-121.0". This is also the spike's main flaw and worth stating:
# these three span roughly 40 km of road while the corridor below is 18 km long, so places
# found near the outer two are legitimately outside it and get dropped. A real run would
# derive names *from* the anchors, keeping the two consistent by construction.
PLACES = ["Chinook Pass", "Cayuse Pass", "Crystal Mountain"]

CATEGORIES = [
    PoiCategory.WILD_CAMP,
    PoiCategory.VIEWPOINT,
    PoiCategory.FOOD,
]

REGION = "Washington State, USA"
"""Disambiguation. The first run matched Cayuse, Oregon and coastal Chinook, WA — neither of
which is the pass. Reverse-geocoding the anchor would supply this; for now it is stated."""


def wrap(text: str, indent: str = "      ") -> str:
    return textwrap.fill(text, width=96, initial_indent=indent, subsequent_indent=indent)


async def main() -> int:
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    ors_key = os.environ.get("ORS_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("MOTOROOTER_LLM_MODEL", "gpt-5-mini")
    places_key = os.environ.get("GOOGLE_MAPS_SERVER_KEY")
    if not brave_key or not ors_key or not openai_key or not places_key:
        print(
            "need BRAVE_SEARCH_API_KEY, ORS_API_KEY, OPENAI_API_KEY and "
            "GOOGLE_MAPS_SERVER_KEY",
            file=sys.stderr,
        )
        return 1

    leg = await OrsProvider(api_key=ors_key).route(
        RouteRequest(waypoints=(START, END), intent=LegIntent.TWISTY_PAVED)
    )
    placed = anchors(leg.geometry)

    print(f"corridor: {leg.distance_m / 1000:.1f} km, {len(leg.geometry)} points")
    print(f"anchors:  {len(placed)} at ~{spacing_of(placed) / 1000:.1f} km spacing")
    print(f"a full run would cost {total_queries(placed, CATEGORIES)} searches")
    print(f"this spike runs {len(PLACES) * len(CATEGORIES)} of them, on named places\n")

    llm = OpenAiClient(api_key=openai_key, model=model)
    source = BraveSearchSource(api_key=brave_key)
    extractor = PlaceExtractor(llm)
    resolver = PlacesResolver(api_key=places_key)
    classifier = CategoryClassifier(llm)
    scorer = CandidateJudge(llm)

    searched = 0
    named = []
    for place in PLACES:
        for query in queries_for(place, CATEGORIES):
            try:
                results = await source.search(query, near=placed[0], limit=3)
            except DiscoveryError as exc:
                print(f"  search failed [{query.category.value}] {place}: {exc}")
                continue
            searched += len(results)
            named.extend(
                await extractor.extract(results, region=REGION, searched_for=query.place)
            )

    try:
        resolved = await resolver.resolve(named, route=leg.geometry)
    except DiscoveryError as exc:
        print(f"resolve failed: {exc}", file=sys.stderr)
        return 1

    resolved = await classifier.classify(resolved)
    uncategorised = [r for r in resolved if r.category is None]
    if uncategorised:
        print(f"{len(uncategorised)} could not be categorised and cannot be pinned:")
        for item in uncategorised:
            print(f"   {item.candidate.name}  (google: {', '.join(item.places_types) or 'none'})")
        print()

    dropped = len(named) - len(resolved)
    rate = dropped / len(named) if named else 0.0
    print(f"{searched} search results -> {len(named)} named -> {len(resolved)} resolved")
    print(f"dropped at resolve: {dropped} ({rate:.0%})")
    if rate > 0.8:
        print("  NOTE: measured once at 83%, and every drop was correct — the names all")
        print("        resolved, they were just 30-1465 km away. Suspect the search")
        print("        anchors before suspecting the filter.")
    print()

    scored = await scorer.judge(resolved, leg)
    if not scored:
        print("nothing scored.")
        return 0

    print(f"{len(scored)} places, best first:\n")
    for rank, item in enumerate(scored, start=1):
        candidate = item.resolved.candidate
        kind = item.resolved.category.value if item.resolved.category else "UNCATEGORISED"
        print(f"{rank:2}. {item.score:.2f}  {candidate.name}  [{kind}]")
        print(f"      why:      {item.reason}")
        print(f"      measured: {_describe(item.evidence)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
