"""Does the blurb hold its voice, and does it ever state something it was not given?

Three questions, in the order they matter:

1. **Does it invent?** The prompt's one hard rule is that it may suggest anything and state
   nothing it was not handed. A place, a road, a region, a distance, a duration, a claim
   about the riding. Every line here is printed next to the exact `TripFacts` that produced
   it, so the check is a reading rather than a guess.
2. **Does the voice hold, or converge?** Repeating one construction across six trips is
   drift even when each line is fine on its own. Six samples show it; one cannot.
3. **Does an empty trip get nudged to begin** rather than described into existence?

The trips differ in the ways the prompt has to cope with rather than being variants of one:
empty, one waypoint, short and paved, long and unpaved, many places saved and none, with and
without chat history, and one outside Washington — every worked example in the prompt is
Washington, so a Utah trip is where borrowed local colour would show up.

Live, through `build_blurb_writer`, so the real timeout and reasoning effort apply and the
latencies describe what ships. Needs OPENAI_API_KEY.

    uv run --project backend python scripts/blurb_voice_check.py

Nothing here becomes a fixture. The suite stays hermetic; this is a measurement whose output
is read by a person.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0] / "backend/src"))

from motorooter.blurb.facts import facts_for  # noqa: E402
from motorooter.blurb.factory import build_blurb_writer  # noqa: E402
from motorooter.blurb.models import Turn  # noqa: E402
from motorooter.planning.discovery.factory import settings_from_env  # noqa: E402
from motorooter.routing.models import (  # noqa: E402
    Coordinate,
    LegIntent,
    RouteLeg,
    Surface,
    SurfaceSpan,
)
from motorooter.trips.models import (  # noqa: E402
    Poi,
    PoiCategory,
    PoiSource,
    Trip,
    TripLeg,
    Waypoint,
    utc_now,
)


def at(lat: float, lon: float, name: str) -> Waypoint:
    return Waypoint(coordinate=Coordinate(lat=lat, lon=lon), name=name)


def poi(name: str, category: PoiCategory, lat: float, lon: float) -> Poi:
    return Poi(
        id=name.lower().replace(" ", "-"),
        name=name,
        category=category,
        coordinate=Coordinate(lat=lat, lon=lon),
        source=PoiSource.PLACES,
    )


def leg(
    points: tuple[Coordinate, ...],
    intent: LegIntent,
    *,
    spans: tuple[SurfaceSpan, ...] = (),
    start: int = 0,
    end: int = 1,
) -> TripLeg:
    return TripLeg(
        intent=intent,
        start_waypoint_index=start,
        end_waypoint_index=end,
        routed=RouteLeg(
            geometry=points,
            distance_m=1000.0,
            duration_s=600.0,
            surface_spans=spans,
            provider="fake",
            intent=intent,
        ),
    )


def trip(name: str, **kwargs: object) -> Trip:
    now = utc_now()
    return Trip(slug=name.lower().replace(" ", "-"), name=name, created_at=now, edited_at=now, **kwargs)  # type: ignore[arg-type]


def empty_trip() -> Trip:
    return trip("Untitled")


def one_waypoint() -> Trip:
    return trip("Just A Pin", waypoints=(at(47.5962, -120.6615, "Leavenworth"),))


def short_paved_out_and_back() -> Trip:
    """Twisties, so no surface is reported at all — Google returns no spans."""
    a, b = Coordinate(lat=47.42, lon=-121.42), Coordinate(lat=47.39, lon=-121.28)
    return trip(
        "Snoqualmie Out And Back",
        waypoints=(
            at(47.42, -121.42, "North Bend"),
            at(47.39, -121.28, "Snoqualmie Pass"),
            at(47.42, -121.42, "North Bend"),
        ),
        legs=(
            leg((a, b), LegIntent.TWISTY_PAVED, start=0, end=1),
            leg((b, a), LegIntent.TWISTY_PAVED, start=1, end=2),
        ),
        default_intent=LegIntent.TWISTY_PAVED,
    )


def long_dirt_loop() -> Trip:
    """The shape the endpoint sees most: multi-leg, mostly dirt, several places saved."""
    a = Coordinate(lat=47.5962, lon=-120.6615)
    b = Coordinate(lat=47.34, lon=-120.58)
    c = Coordinate(lat=47.19, lon=-120.95)
    return trip(
        "Leavenworth Dirt Loop",
        waypoints=(
            at(47.5962, -120.6615, "Leavenworth"),
            at(47.34, -120.58, "Blewett Pass"),
            at(47.19, -120.95, "Ellensburg"),
            at(47.5962, -120.6615, "Leavenworth"),
        ),
        legs=(
            leg(
                (a, b),
                LegIntent.UNPAVED,
                spans=(SurfaceSpan(start_index=0, end_index=1, surface=Surface.UNPAVED),),
                start=0,
                end=1,
            ),
            leg((b, c), LegIntent.UNPAVED, start=1, end=2),
            leg(
                (c, a),
                LegIntent.HIGHWAY_CONNECTOR,
                spans=(SurfaceSpan(start_index=0, end_index=1, surface=Surface.PAVED),),
                start=2,
                end=3,
            ),
        ),
        pois=(
            poi("Eightmile Campground", PoiCategory.CAMPGROUND, 47.55, -120.75),
            poi("Halfway Flat", PoiCategory.WILD_CAMP, 47.5, -120.6),
            poi("South Cle Elum Diner", PoiCategory.FOOD, 47.18, -120.94),
            poi("Blewett Summit Viewpoint", PoiCategory.VIEWPOINT, 47.34, -120.58),
            poi("Cle Elum Fuel", PoiCategory.FUEL, 47.19, -120.94),
            poi("Icicle Creek Camp", PoiCategory.WILD_CAMP, 47.53, -120.72),
        ),
        default_intent=LegIntent.UNPAVED,
    )


def utah_dirt() -> Trip:
    """Not Washington. Every worked example in the prompt is, so borrowed colour shows here."""
    a = Coordinate(lat=38.5733, lon=-109.5498)
    b = Coordinate(lat=38.6402, lon=-109.7)
    return trip(
        "Moab Dirt Run",
        waypoints=(at(38.5733, -109.5498, "Moab"), at(38.6402, -109.7, "Gemini Bridges")),
        legs=(
            leg(
                (a, b),
                LegIntent.UNPAVED,
                spans=(SurfaceSpan(start_index=0, end_index=1, surface=Surface.UNPAVED),),
            ),
        ),
        pois=(poi("Kane Creek Campground", PoiCategory.CAMPGROUND, 38.55, -109.6),),
        default_intent=LegIntent.UNPAVED,
    )


HISTORY = (
    Turn(role="user", content="somewhere to swim on this? it is going to be roasting"),
    Turn(role="assistant", content="I have added Blewett Pass. Want me to look for water?"),
)

CASES: tuple[tuple[str, Trip, tuple[Turn, ...]], ...] = (
    ("empty trip, nothing placed", empty_trip(), ()),
    ("one waypoint, nothing else", one_waypoint(), ()),
    ("short paved out-and-back, no places saved", short_paved_out_and_back(), ()),
    ("long dirt loop, 6 places saved", long_dirt_loop(), ()),
    ("the same dirt loop, WITH chat history", long_dirt_loop(), HISTORY),
    ("Moab, Utah — dirt, 1 place saved", utah_dirt(), ()),
)


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set; this makes live calls and cannot run.")
        raise SystemExit(1)

    writer = build_blurb_writer(settings_from_env())
    if writer is None:
        print("no blurb writer built; check the OpenAI key.")
        raise SystemExit(1)

    latencies: list[float] = []
    for description, document, history in CASES:
        facts = facts_for(document)
        started = time.monotonic()
        line = await writer.write(document, history)
        elapsed = time.monotonic() - started
        latencies.append(elapsed)

        print(f"\n{'=' * 78}\n{description}   [{elapsed:.1f}s]")
        print("  facts it was given:")
        for field, value in dataclasses.asdict(facts).items():
            if value not in ((), None, {}, 0, False):
                print(f"    {field}: {value}")
        if history:
            print(f"    history: {[turn.content for turn in history]}")
        print(f"\n  LINE: {line!r}")

    print(f"\n{'=' * 78}")
    print(f"latency: min {min(latencies):.1f}s  max {max(latencies):.1f}s  n={len(latencies)}")


if __name__ == "__main__":
    asyncio.run(main())
