"""What is WABDR Section 3 actually made of, in OpenStreetMap terms?

Routing quality is the riskiest unvalidated assumption in this project: hosted
OpenRouteService has no motorcycle profile, so dirt legs currently route through
`cycling-mountain`, which reaches tracks a car profile refuses but applies *bicycle* access
rules. Whether that approximation is acceptable depends entirely on how the roads a real BDR
uses are tagged.

This samples points along the reference GPX track, asks Overpass what way each point sits
on, and reports the tag distribution. Three questions:

  1. What surface is it? Confirms the route really is mostly unpaved.
  2. Would `driving-car` refuse it? `highway=track` and rough `tracktype` values are what a
     car profile avoids — that is *why* we reach for cycling-mountain.
  3. Would `cycling-mountain` be WRONG? Ways tagged `highway=path`/`bridleway`/`cycleway`,
     or with `motor_vehicle=no`, are bicycle-legal and motorcycle-illegal. If the real route
     needs those, a bicycle profile is not an approximation, it is a different road network.

Needs no API key — Overpass is open. Run:

    uv run --project backend python scripts/wabdr_osm_audit.py [--track "WA3 -"] [--every-m 2000]
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from itertools import pairwise

OVERPASS = "https://overpass-api.de/api/interpreter"
SEARCH_RADIUS_M = 25
"""How far from a sampled point to look for a way. GPS traces wander; 30 m is generous
without reaching a parallel road in most terrain."""

BATCH = 20
"""Sample points per Overpass request. Keeps each query small enough to be polite."""

# Bicycle-legal, motorcycle-illegal. If the reference route runs on these, a bicycle profile
# is not approximating a moto profile — it is routing on a different network.
MOTO_ILLEGAL_HIGHWAYS = {
    "path",
    "bridleway",
    "cycleway",
    "footway",
    "steps",
    "pedestrian",
}
PAVED_SURFACES = {"asphalt", "paved", "concrete", "paving_stones", "chipseal", "sett"}
UNPAVED_SURFACES = {
    "unpaved",
    "gravel",
    "dirt",
    "ground",
    "compacted",
    "fine_gravel",
    "sand",
    "grass",
    "earth",
    "mud",
    "rock",
    "pebblestone",
    "woodchips",
}


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def load_track(gpx: pathlib.Path, prefix: str) -> list[tuple[float, float]]:
    text = gpx.read_text(errors="replace")
    for block in re.findall(r"<trk>(.*?)</trk>", text, re.DOTALL):
        name = re.search(r"<name>(.*?)</name>", block, re.DOTALL)
        if name and name.group(1).startswith(prefix):
            return [
                (float(la), float(lo))
                for la, lo in re.findall(
                    r'<trkpt lat="([-\d.]+)" lon="([-\d.]+)"', block
                )
            ]
    raise SystemExit(f"no track starting {prefix!r} in {gpx}")


def sample(
    points: list[tuple[float, float]], every_m: float
) -> list[tuple[float, float]]:
    """Points spaced along the track, so the audit weights by distance, not by GPS density."""
    picked = [points[0]]
    travelled = 0.0
    for a, b in pairwise(points):
        travelled += haversine_m(a, b)
        if travelled >= every_m:
            picked.append(b)
            travelled = 0.0
    return picked


def query(
    points: list[tuple[float, float]], attempt: int = 1
) -> list[dict[str, object]]:
    """Ways near these points, WITH geometry so the nearest one can be picked locally.

    `out geom` rather than `out center`: a 30 m radius returns every way in range, and near
    a town that is a dozen footways and a trunk road. Only the way the track actually runs
    along is interesting, and finding it needs real point-to-segment distance.
    """
    clauses = "".join(
        f"way(around:{SEARCH_RADIUS_M},{lat},{lon})[highway];" for lat, lon in points
    )
    body = f"[out:json][timeout:90];({clauses});out tags geom;"
    request = urllib.request.Request(
        OVERPASS,
        data=urllib.parse.urlencode({"data": body}).encode(),
        headers={"User-Agent": "motorooter-routing-spike/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return list(json.loads(response.read()).get("elements", []))
    except urllib.error.HTTPError as exc:
        # Overpass is a free shared service and 429s under load. Backing off and retrying
        # matters: a dropped batch silently biases the sample toward whatever survived.
        if exc.code in (429, 504) and attempt <= 4:
            wait = 15 * attempt
            print(f"    {exc.code} from Overpass, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            return query(points, attempt + 1)
        raise


def point_to_segment_m(p, a, b) -> float:
    """Approximate metres from p to segment a-b, in a local flat projection."""
    scale = math.cos(math.radians(p[0]))
    px, py = p[1] * scale, p[0]
    ax, ay = a[1] * scale, a[0]
    bx, by = b[1] * scale, b[0]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return haversine_m(p, (ay + t * dy, (ax + t * dx) / scale))


def nearest_way(point, ways):
    """The single way the track is actually running along at this point."""
    best, best_d = None, float("inf")
    for way in ways:
        geom = way.get("geometry") or []
        for a, b in pairwise(geom):
            d = point_to_segment_m(point, (a["lat"], a["lon"]), (b["lat"], b["lon"]))
            if d < best_d:
                best, best_d = way, d
    return (best, best_d) if best_d <= SEARCH_RADIUS_M else (None, best_d)


def classify_surface(tags: dict[str, str]) -> str:
    surface = tags.get("surface", "")
    if surface in PAVED_SURFACES:
        return "paved"
    if surface in UNPAVED_SURFACES:
        return "unpaved"
    if surface:
        return f"other:{surface}"
    # No surface tag. highway=track is unpaved by overwhelming convention.
    return "untagged(track)" if tags.get("highway") == "track" else "untagged"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gpx",
        type=pathlib.Path,
        default=pathlib.Path.home() / "Downloads/WABDR-Nov2025.gpx",
    )
    parser.add_argument("--track", default="WA3 -")
    parser.add_argument("--every-m", type=float, default=2000.0)
    args = parser.parse_args()

    track = load_track(args.gpx, args.track)
    points = sample(track, args.every_m)
    total_km = sum(haversine_m(a, b) for a, b in pairwise(track)) / 1000
    print(f"{args.track.strip()} — {len(track)} pts, {total_km:.1f} km")
    print(
        f"sampling every {args.every_m:.0f} m -> {len(points)} probes, {SEARCH_RADIUS_M} m radius\n"
    )

    ways: list[dict[str, object]] = []
    for i in range(0, len(points), BATCH):
        batch = points[i : i + BATCH]
        try:
            ways.extend(query(batch))
        except (urllib.error.URLError, TimeoutError) as exc:
            print(
                f"  batch {i // BATCH + 1}: FAILED ({exc}) — partial results",
                file=sys.stderr,
            )
            continue
        print(
            f"  batch {i // BATCH + 1}/{(len(points) + BATCH - 1) // BATCH}: {len(ways)} ways so far"
        )
        time.sleep(2)  # Overpass is a free shared service; do not hammer it.

    # Per-probe, not per-distinct-way. Each probe stands for the same length of track, so
    # counting probes weights the answer by DISTANCE. Counting distinct ways would let a
    # 40 m residential stub at Cashmere outvote 20 km of forest road.
    matched: list[dict[str, str]] = []
    unmatched = 0
    for point in points:
        way, _distance = nearest_way(point, ways)
        if way is None:
            unmatched += 1
            continue
        tags = way.get("tags") or {}
        matched.append(tags)  # type: ignore[arg-type]

    if not matched:
        print("\nNo probes matched a way — Overpass may be rate limiting. Retry later.")
        return 1

    n = len(matched)
    km_each = total_km / len(points)
    print(f"\n{n}/{len(points)} probes matched a way ({unmatched} unmatched)")
    print(f"each probe stands for ~{km_each:.1f} km\n")

    highways = Counter(t.get("highway", "?") for t in matched)
    surfaces = Counter(classify_surface(t) for t in matched)
    tracktypes = Counter(t.get("tracktype", "untagged") for t in matched)

    def show(title: str, counter: Counter[str]) -> None:
        print(title)
        for key, count in counter.most_common():
            print(
                f"    {key:<18} {count:>3}  {100 * count / n:>4.0f}%  {count * km_each:>5.1f} km"
            )

    show("highway=", highways)
    show("surface class", surfaces)
    show("tracktype=", tracktypes)

    print("\n--- the three questions ---")
    unpaved = surfaces["unpaved"] + surfaces["untagged(track)"]
    print(
        f"1. unpaved-ish     {100 * unpaved / n:.0f}% of distance ({unpaved * km_each:.0f} km)"
    )

    car_avoids = highways["track"]
    print(
        f"2. highway=track   {100 * car_avoids / n:.0f}% ({car_avoids * km_each:.0f} km)"
        "   <- what driving-car avoids"
    )

    illegal = [t for t in matched if t.get("highway") in MOTO_ILLEGAL_HIGHWAYS]
    blocked = [t for t in matched if t.get("motor_vehicle") in {"no", "private"}]
    print(
        f"3. moto-illegal    {100 * len(illegal) / n:.0f}% bicycle-only by highway type"
        f" ({len(illegal)} probes)"
    )
    print(f"   motor_vehicle=no/private: {len(blocked)} probes")
    for t in illegal[:6]:
        print(
            f"     highway={t.get('highway')} surface={t.get('surface', '-')} name={t.get('name', '-')}"
        )

    access = Counter(t.get("access", "untagged") for t in matched)
    print(f"\naccess=            {dict(access.most_common(5))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
