"""M0: does our routing reproduce a real Backcountry Discovery Route?

The riskiest unvalidated assumption in this project. Hosted OpenRouteService has no
motorcycle profile, so dirt legs route through `cycling-mountain` — a profile that reaches
tracks a car profile refuses, but applies bicycle rules. If that does not produce roads a
rider would actually take, the premise is wrong and everything above it is built on sand.

`wabdr_osm_audit.py` already answered the legality half without a key: WABDR Section 3 is
essentially all motor-vehicle-legal, so the fear of routing onto hiking trails does not
materialise. This answers the harder half — given only the two endpoints, does the engine
*find* the BDR, or does it find some other way across the mountains?

Method: route endpoint-to-endpoint through each profile, then measure how far each result
strays from the reference GPX track. Deviation is the headline: an engine that produces a
plausible 120 km route down the highway has failed, and distance alone would not show it.

    uv run --project backend python scripts/routing_spike.py

Writes GPX for every candidate beside the reference so they can be opened together.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from itertools import pairwise

ORS_BASE = "https://api.openrouteservice.org"
OUT_DIR = pathlib.Path("/tmp/motorooter-spike")

PROFILES = ["cycling-mountain", "driving-car"]
"""What we use for dirt today, and what we would get without it."""

NEAR_M = 100.0
"""A route within this of the reference is on the same road, allowing for survey drift."""

SURFACE_CODES = {
    0: "unknown",
    1: "paved",
    2: "unpaved",
    3: "asphalt",
    4: "concrete",
    5: "cobblestone",
    6: "metal",
    7: "wood",
    8: "compacted gravel",
    9: "fine gravel",
    10: "gravel",
    11: "dirt",
    12: "ground",
    13: "ice",
    14: "paving stones",
    15: "sand",
    16: "woodchips",
    17: "grass",
    18: "grass paver",
}
UNPAVED = {2, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18}


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def length_m(points: list[tuple[float, float]]) -> float:
    return sum(haversine_m(a, b) for a, b in pairwise(points))


def load_track(gpx: pathlib.Path, prefix: str) -> list[tuple[float, float]]:
    for block in re.findall(
        r"<trk>(.*?)</trk>", gpx.read_text(errors="replace"), re.DOTALL
    ):
        name = re.search(r"<name>(.*?)</name>", block, re.DOTALL)
        if name and name.group(1).startswith(prefix):
            return [
                (float(la), float(lo))
                for la, lo in re.findall(
                    r'<trkpt lat="([-\d.]+)" lon="([-\d.]+)"', block
                )
            ]
    raise SystemExit(f"no track starting {prefix!r} in {gpx}")


class Reference:
    """Reference track with a grid index, so deviation is not an O(n*m) scan."""

    CELL = 0.01  # ~1.1 km of latitude; deviations of interest are far smaller.

    def __init__(self, points: list[tuple[float, float]]) -> None:
        self.points = points
        self.grid: dict[tuple[int, int], list[int]] = {}
        for i, (lat, lon) in enumerate(points):
            self.grid.setdefault(
                (int(lat / self.CELL), int(lon / self.CELL)), []
            ).append(i)

    def distance_to(self, point: tuple[float, float]) -> float:
        lat, lon = point
        cy, cx = int(lat / self.CELL), int(lon / self.CELL)
        best = float("inf")
        # Widen until something is found: a route that has wandered far from the reference
        # is exactly the case worth measuring accurately.
        for ring in range(1, 40):
            for dy in range(-ring, ring + 1):
                for dx in range(-ring, ring + 1):
                    if max(abs(dy), abs(dx)) != ring - 1 and ring > 1:
                        continue
                    for i in self.grid.get((cy + dy, cx + dx), ()):
                        best = min(best, haversine_m(point, self.points[i]))
            if best < ring * self.CELL * 111_000:
                break
        return best


def route(profile: str, waypoints: list[tuple[float, float]], key: str) -> dict:
    body = json.dumps(
        {
            "coordinates": [[lo, la] for la, lo in waypoints],
            "extra_info": ["surface"],
            "elevation": True,
        }
    ).encode()
    request = urllib.request.Request(
        f"{ORS_BASE}/v2/directions/{profile}/geojson",
        data=body,
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return dict(json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        return {
            "__error__": f"HTTP {exc.code}: {exc.read()[:300].decode(errors='replace')}"
        }


def write_gpx(path: pathlib.Path, name: str, points: list[tuple[float, float]]) -> None:
    body = "".join(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"/>' for la, lo in points)
    path.write_text(
        '<?xml version="1.0"?><gpx version="1.1" creator="motorooter-spike" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>{name}</name><trkseg>{body}</trkseg></trk></gpx>"
    )


def summarize(profile: str, payload: dict, reference: Reference, ref_km: float) -> None:
    if "__error__" in payload:
        print(f"\n{profile}: FAILED — {payload['__error__']}")
        return
    features = payload.get("features") or []
    if not features:
        print(f"\n{profile}: no route returned")
        return

    feature = features[0]
    coords = feature["geometry"]["coordinates"]
    points = [(c[1], c[0]) for c in coords]
    props = feature["properties"]
    km = props["summary"]["distance"] / 1000
    hours = props["summary"]["duration"] / 3600

    spans = props.get("extras", {}).get("surface", {}).get("values", [])
    unpaved_m = paved_m = 0.0
    breakdown: dict[str, float] = {}
    for start, end, code in spans:
        seg = length_m(points[start : min(end, len(points) - 1) + 1])
        breakdown[SURFACE_CODES.get(code, str(code))] = (
            breakdown.get(SURFACE_CODES.get(code, str(code)), 0.0) + seg
        )
        if code in UNPAVED:
            unpaved_m += seg
        elif code != 0:
            paved_m += seg

    deviations = sorted(reference.distance_to(p) for p in points)
    near = sum(1 for d in deviations if d <= NEAR_M)
    median = deviations[len(deviations) // 2]
    p90 = deviations[int(len(deviations) * 0.9)]

    print(f"\n{profile}")
    print(
        f"  distance      {km:.1f} km   (reference {ref_km:.1f} km, {100 * km / ref_km:.0f}%)"
    )
    print(f"  duration      {hours:.1f} h")
    print(f"  ascent        {props.get('ascent', 0):.0f} m")
    print(
        f"  ORS surface   unpaved {unpaved_m / 1000:.1f} km, paved {paved_m / 1000:.1f} km"
        f"  ({100 * unpaved_m / max(unpaved_m + paved_m, 1):.0f}% unpaved of tagged)"
    )
    if breakdown:
        top = sorted(breakdown.items(), key=lambda kv: -kv[1])[:5]
        print("                " + ", ".join(f"{k} {v / 1000:.0f}km" for k, v in top))
    print(
        f"  FOLLOWS BDR   {100 * near / len(points):.0f}% of points within {NEAR_M:.0f} m"
    )
    print(f"                median deviation {median:.0f} m, p90 {p90:.0f} m")

    out = OUT_DIR / f"{profile}.gpx"
    write_gpx(out, f"MotoRooter {profile}", points)
    print(f"  wrote         {out}")


def main() -> int:
    key = os.environ.get("ORS_API_KEY")
    if not key:
        env = pathlib.Path(__file__).resolve().parents[1] / "backend/.env"
        if env.is_file():
            match = re.search(r"^ORS_API_KEY=(.+)$", env.read_text(), re.MULTILINE)
            key = match.group(1).strip() if match else None
    if not key:
        print("no ORS_API_KEY (env or backend/.env)", file=sys.stderr)
        return 1

    gpx = pathlib.Path.home() / "Downloads/WABDR-Nov2025.gpx"
    track = load_track(gpx, "WA3 -")
    ref_km = length_m(track) / 1000
    start, end = track[0], track[-1]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_gpx(OUT_DIR / "reference-wa3.gpx", "WABDR WA3 reference", track)

    print(f"WABDR WA3 reference: {ref_km:.1f} km, {len(track)} points")
    print(f"  start {start[0]:.5f},{start[1]:.5f}   end {end[0]:.5f},{end[1]:.5f}")
    print(f"  routing endpoint-to-endpoint, no intermediate hints\n{'=' * 62}")

    reference = Reference(track)

    # Endpoint-to-endpoint first: can the engine FIND the BDR unaided?
    for profile in PROFILES:
        summarize(
            f"{profile} (endpoints only)",
            route(profile, [start, end], key),
            reference,
            ref_km,
        )

    # Then the way the product will actually work. The LLM proposes waypoints along a known
    # route and the engine connects them; it is never asked to rediscover a curated route
    # from nothing. Hints are sampled evenly from the reference, standing in for waypoints a
    # model would name (a pass, a forest road junction, a town).
    print(
        f"\n{'=' * 62}\nWith intermediate waypoints — the real usage pattern\n{'=' * 62}"
    )
    for hint_count in (3, 8, 20):
        step = len(track) // (hint_count + 1)
        hints = (
            [track[0]]
            + [track[i * step] for i in range(1, hint_count + 1)]
            + [track[-1]]
        )
        summarize(
            f"cycling-mountain ({hint_count} hints)",
            route("cycling-mountain", hints, key),
            reference,
            ref_km,
        )

    print(f"\nGPX written to {OUT_DIR} — open alongside the reference to compare.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
