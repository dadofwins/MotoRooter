"""What does discovery actually find along one corridor?

The M0 routing spike worked because it produced something a rider could open and judge in a
minute. This is the same idea for the half of the product that has no reference to check
against: there is no WABDR track that says "yes, that is a good place to camp". Tim is the
reference, so the output has to be something he can read and say "no, I would not stop
there" — before any of it is wired into scoring.

Search and extract. Resolve and judge print as TODO so the shape stays visible.

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
from motorooter.planning.discovery.queries import queries_for, total_queries  # noqa: E402
from motorooter.planning.discovery.sources.brave import BraveSearchSource  # noqa: E402
from motorooter.routing.models import Coordinate, LegIntent, RouteRequest  # noqa: E402
from motorooter.routing.providers.ors import OrsProvider  # noqa: E402
from motorooter.trips.models import PoiCategory  # noqa: E402

# A short, real, interesting section: Chinook Pass. Twisty, paved, and the sort of road
# where the difference between a good and a bad recommendation is obvious to a rider.
START = Coordinate(lat=46.9720, lon=-121.5340)
END = Coordinate(lat=46.8722, lon=-121.5165)

# Named places along it. Turning an anchor coordinate into a place name is the next stage to
# build — Brave cannot search "47.0,-121.0" — so for now the names are supplied by hand and
# the anchors only decide how many of them a real corridor would need.
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
    if not brave_key or not ors_key or not openai_key:
        print("need BRAVE_SEARCH_API_KEY, ORS_API_KEY and OPENAI_API_KEY", file=sys.stderr)
        return 1

    leg = await OrsProvider(api_key=ors_key).route(
        RouteRequest(waypoints=(START, END), intent=LegIntent.TWISTY_PAVED)
    )
    placed = anchors(leg.geometry)

    print(f"corridor: {leg.distance_m / 1000:.1f} km, {len(leg.geometry)} points")
    print(f"anchors:  {len(placed)} at ~{spacing_of(placed) / 1000:.1f} km spacing")
    print(f"a full run would cost {total_queries(placed, CATEGORIES)} searches")
    print(f"this spike runs {len(PLACES) * len(CATEGORIES)} of them, on named places\n")

    source = BraveSearchSource(api_key=brave_key)
    extractor = PlaceExtractor(OpenAiClient(api_key=openai_key, model=model))

    searched = 0
    extracted = 0
    for place in PLACES:
        for query in queries_for(place, CATEGORIES):
            try:
                results = await source.search(query, near=placed[0], limit=3)
            except DiscoveryError as exc:
                print(f"  [{query.category.value}] {query.text}\n      FAILED: {exc}\n")
                continue

            searched += len(results)
            print(f"  [{query.category.value}] {query.text}")
            for result in results:
                print(f"      page:  {result.name}")

            named = await extractor.extract(
                results, region=REGION, searched_for=query.place
            )
            extracted += len(named)
            if not named:
                print("      -> no place named\n")
                continue
            for candidate in named:
                print(f"      PLACE: {candidate.name}")
                if candidate.snippet:
                    print(wrap(candidate.snippet, indent="             "))
            print()

    print(f"{searched} search results -> {extracted} named places.")
    print("All still unverified: none has a place_id or a real coordinate.")
    print("TODO resolve: Places lookup turns each name into a place_id, or drops it.")
    print("TODO judge:   computed metrics plus the snippets above, scored with a reason.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
