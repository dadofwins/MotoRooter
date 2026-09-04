"""What reasoning effort should the rail's blurb run at, and how long does it take?

Two stages of the discovery pipeline wanted opposite settings — extraction got the same
answer from `minimal` in a tenth of the time, while judging scored the same candidate 0.90
at the default and 0.45 at `low` — so the setting is per-stage and guessing was wrong in
both directions. This measures the blurb rather than inheriting either answer.

Live: it calls OpenAI through `BlurbWriter`, which is the production path, so the numbers
describe the thing that ships rather than a script shaped like it. Needs OPENAI_API_KEY.

    uv run --project backend python scripts/blurb_effort_spike.py

The trip is a small routed Leavenworth loop with one saved camp — Tim's example, and the
shape the endpoint will see most often.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0] / "backend/src"))

from motorooter.blurb.writer import BlurbWriter  # noqa: E402
from motorooter.llm.providers.openai import OpenAiClient  # noqa: E402
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

RUNS = 5
TIMEOUT_S = 60.0
"""Generous on purpose: the point is to see the default's real latency, not to cut it off."""


def a_leavenworth_loop() -> Trip:
    now = utc_now()
    leg = RouteLeg(
        geometry=(
            Coordinate(lat=47.5962, lon=-120.6615),
            Coordinate(lat=47.34, lon=-120.58),
            Coordinate(lat=47.5962, lon=-120.6615),
        ),
        distance_m=118_000.0,
        duration_s=9_000.0,
        surface_spans=(SurfaceSpan(start_index=0, end_index=1, surface=Surface.UNPAVED),),
        provider="ors",
        intent=LegIntent.UNPAVED,
    )
    return Trip(
        slug="leavenworth-loop",
        name="Leavenworth Loop",
        created_at=now,
        edited_at=now,
        waypoints=(
            Waypoint(coordinate=Coordinate(lat=47.5962, lon=-120.6615), name="Leavenworth"),
            Waypoint(coordinate=Coordinate(lat=47.34, lon=-120.58), name="Blewett Pass"),
            Waypoint(coordinate=Coordinate(lat=47.5962, lon=-120.6615), name="Leavenworth"),
        ),
        legs=(
            TripLeg(
                intent=LegIntent.UNPAVED,
                start_waypoint_index=0,
                end_waypoint_index=1,
                routed=leg,
            ),
            TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=1, end_waypoint_index=2),
        ),
        pois=(
            Poi(
                id="eightmile",
                name="Eightmile Campground",
                category=PoiCategory.CAMPGROUND,
                coordinate=Coordinate(lat=47.55, lon=-120.75),
                source=PoiSource.PLACES,
            ),
        ),
    )


async def measure(effort: str | None, trip: Trip) -> None:
    settings = settings_from_env()
    writer = BlurbWriter(
        OpenAiClient(
            api_key=settings.openai_api_key or "",
            model=settings.model,
            timeout_s=TIMEOUT_S,
            reasoning_effort=effort,
        )
    )
    label = effort or "default (unset)"
    print(f"\n--- reasoning_effort: {label} ---")
    for _ in range(RUNS):
        started = time.monotonic()
        line = await writer.write(trip)
        elapsed = time.monotonic() - started
        print(f"  {elapsed:5.1f}s  {line!r}")


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set; this spike makes live calls and cannot run.")
        raise SystemExit(1)
    trip = a_leavenworth_loop()
    for effort in ("minimal", None):
        await measure(effort, trip)


if __name__ == "__main__":
    asyncio.run(main())
