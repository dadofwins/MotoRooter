"""Is `GAP_REPORT_THRESHOLD_M` in an empty band, or on a slope?

    uv run --project backend python scripts/gap_threshold_check.py

Requires ORS_API_KEY and GOOGLE_MAPS_SERVER_KEY.

Does a boundary gap ever land near 25 m, or is it always tiny or enormous?

A threshold only matters where the data is ambiguous. With waypoints on roads, cross-engine
boundaries disagree by about 2 m; with waypoints picked off a map, by kilometres. This sweeps
between: nudge each shared waypoint a known distance off the road and see what the boundary
does.

If gaps jump straight from metres to hundreds of metres, 25 is safe anywhere in a wide band
and the number never needed deriving. If they pass smoothly through 25, the value matters and
the docstring is right to call it provisional.

Measured 2026-08-26: bimodal, with nothing near the threshold. Kept so the next person can
re-run it against a different corridor rather than re-derive the setup — the setup is the
hard part, and getting it wrong is easy. A first attempt used waypoints picked off a map and
measured up to 2.5 km of "engine disagreement" that was entirely both engines snapping away
from somewhere no road goes. Waypoints must come *from* a routed line.
"""

import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, "backend/src")
for line in Path("backend/.env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from motorooter.routing.factory import RoutingSettings, build_routing
from motorooter.routing.geo import haversine_m
from motorooter.routing.models import Coordinate, LegIntent, RouteRequest

START = Coordinate(lat=46.9720, lon=-121.5340)
END = Coordinate(lat=46.7300, lon=-120.9400)
OFFSETS_M = [0, 25, 100, 400, 1600]
M_PER_DEGREE = 111_320.0


async def main() -> None:
    _, resolver = build_routing(
        RoutingSettings(
            ors_api_key=os.environ["ORS_API_KEY"],
            google_api_key=os.environ["GOOGLE_MAPS_SERVER_KEY"],
        )
    )
    paved = resolver.resolve(LegIntent.TWISTY_PAVED)
    dirt = resolver.resolve(LegIntent.UNPAVED)

    spine = await paved.route(RouteRequest(waypoints=(START, END), intent=LegIntent.TWISTY_PAVED))
    middle = spine.geometry[len(spine.geometry) // 2]
    print(f"shared waypoint from a routed line: {middle.lat:.5f},{middle.lon:.5f}\n")
    print(f"{'nudge':>8}  {'google end':>12}  {'ors start':>12}  {'boundary gap':>14}  verdict")

    for offset in OFFSETS_M:
        shifted = Coordinate(lat=middle.lat + offset / M_PER_DEGREE, lon=middle.lon)
        first = await paved.route(
            RouteRequest(waypoints=(START, shifted), intent=LegIntent.TWISTY_PAVED)
        )
        second = await dirt.route(
            RouteRequest(waypoints=(shifted, END), intent=LegIntent.UNPAVED)
        )
        gap = haversine_m(first.geometry[-1], second.geometry[0])
        verdict = "reported" if gap > 25.0 else "silent"
        print(
            f"{offset:6} m  {haversine_m(shifted, first.geometry[-1]):10.1f} m  "
            f"{haversine_m(shifted, second.geometry[0]):10.1f} m  {gap:12.1f} m  {verdict}"
        )

asyncio.run(main())
