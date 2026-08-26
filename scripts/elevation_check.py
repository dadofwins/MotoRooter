#!/usr/bin/env python3
"""Why the reported ascent for WABDR Section 3 disagrees with the published figure.

`CLAUDE.md` carried a standing note from M0: hosted ORS reported 6,400-8,800 m of climb for
WABDR Section 3 against the published track's 3,188 m, cause unknown, and climb must not be
shown to a rider until someone checks. Two benign explanations were on the table — a
definitional difference in how ascent is summed, and the much denser geometry ORS returns.
This script rules out both, which leaves the interesting ones.

Run it:

    python3 scripts/elevation_check.py

No API keys and no network. It reads the reference GPX and computes everything locally.

**The reference track is licensed content and is deliberately not in this repository.** BDR
GPX files may not be vendored, so this reads one from `~/Downloads` and writes nothing derived
from it. If the file is missing the script says so and stops; do not "fix" that by committing
the track.

What it found, 2026-08-26:

    naive cumulative ascent, native sampling      3188 m   <- exactly the published figure
    with a 5 m noise threshold                    2909 m
    with a 30 m noise threshold                   2560 m

So the published 3,188 m *is* the naive sum of positive deltas. The gap is not "they smoothed
and we did not" — both numbers are the same kind of number.

    every  1 point ->  112 m spacing -> 3188 m
    every  4 points ->  446 m spacing -> 2781 m
    every 16 points -> 1784 m spacing -> 2479 m

Ascent goes as roughly spacing^-0.08: thinning by sixteen costs only 22%. Extrapolated to the
~29 m spacing ORS actually returns, that predicts about 3,558 m — so sampling density accounts
for a few hundred metres of a multi-thousand-metre gap, not for the gap.

What remains is one of two things, and they need different actions: either `cycling-mountain`
routes a materially steeper line than the BDR, in which case `ascent_m` is correct for the
route ORS chose and the *route* is the problem; or ORS's elevation lookup or accumulation is
wrong, which is a provider bug worth reporting upstream. Separating them needs the ORS line's
own per-point elevations, which do not currently cross the API boundary.
"""

from __future__ import annotations

import math
import pathlib
import re
import statistics
import sys

REFERENCE = pathlib.Path.home() / "Downloads" / "WABDR-Nov2025.gpx"
SECTION = "WA3 -"

#: Published ascent for the section, for comparison. Not derived here.
PUBLISHED_ASCENT_M = 3188

#: Mean point spacing hosted ORS returned for a real leg: 1406 points over 40.3 km.
ORS_SPACING_M = 29.0

Point = tuple[float, float, float]


def load_section(gpx: pathlib.Path, prefix: str) -> list[Point]:
    """Points of the named track, as (lat, lon, elevation)."""
    text = gpx.read_text(errors="replace")
    for block in re.findall(r"<trk>(.*?)</trk>", text, re.DOTALL):
        name = re.search(r"<name>(.*?)</name>", block, re.DOTALL)
        if name is None or not name.group(1).startswith(prefix):
            continue
        return [
            (float(lat), float(lon), float(ele))
            for lat, lon, ele in re.findall(
                r'<trkpt lat="([-\d.]+)" lon="([-\d.]+)"[^>]*>\s*<ele>([-\d.]+)</ele>',
                block,
            )
        ]
    return []


def haversine_m(a: Point, b: Point) -> float:
    radius = 6_371_008.8
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    d_lat = lat2 - lat1
    d_lon = math.radians(b[1] - a[1])
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def naive_ascent(elevations: list[float]) -> float:
    """Sum of every positive step. The definition both figures under comparison use."""
    return sum(max(0.0, b - a) for a, b in zip(elevations, elevations[1:]))


def thresholded_ascent(elevations: list[float], threshold: float) -> float:
    """Ascent counting only climbs that exceed `threshold`, to bound the effect of DEM noise."""
    total = 0.0
    anchor = elevations[0]
    for value in elevations[1:]:
        if value > anchor:
            if value - anchor > threshold:
                total += value - anchor
                anchor = value
        elif anchor - value > threshold:
            anchor = value
    return total


def main() -> int:
    if not REFERENCE.exists():
        print(f"reference track not found at {REFERENCE}", file=sys.stderr)
        print(
            "BDR tracks are licensed and are deliberately not committed — put the file there "
            "by hand rather than adding it to the repository.",
            file=sys.stderr,
        )
        return 1

    points = load_section(REFERENCE, SECTION)
    if not points:
        print(f"no track starting {SECTION!r} with elevations in {REFERENCE}", file=sys.stderr)
        return 1

    length_m = sum(haversine_m(a, b) for a, b in zip(points, points[1:]))
    elevations = [point[2] for point in points]
    print(
        f"{SECTION.strip(' -')}: {len(points)} points over {length_m / 1000:.1f} km "
        f"({length_m / len(points):.0f} m mean spacing), "
        f"{min(elevations):.0f}-{max(elevations):.0f} m"
    )

    print("\nIs the published figure a smoothed number? Ascent by noise threshold:")
    for threshold in (0, 1, 2, 5, 10, 20, 30):
        ascent = thresholded_ascent(elevations, float(threshold))
        note = "  <- the published figure" if threshold == 0 else ""
        print(f"  {threshold:>2} m -> {ascent:>7.0f} m{note}")
    print(f"  published: {PUBLISHED_ASCENT_M} m")

    print("\nDoes sampling density explain the gap? Ascent as the track is thinned:")
    samples: list[tuple[float, float]] = []
    for step in (1, 2, 3, 4, 6, 8, 12, 16):
        thinned = points[::step]
        spacing = length_m / max(1, len(thinned) - 1)
        ascent = naive_ascent([point[2] for point in thinned])
        samples.append((spacing, ascent))
        print(f"  every {step:>2} -> {spacing:>5.0f} m spacing -> {ascent:>6.0f} m")

    xs = [math.log(spacing) for spacing, _ in samples]
    ys = [math.log(ascent) for _, ascent in samples]
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sum(
        (x - mean_x) ** 2 for x in xs
    )
    base_spacing, base_ascent = samples[0]
    predicted = base_ascent * (ORS_SPACING_M / base_spacing) ** slope
    print(f"\n  ascent ~ spacing^{slope:.2f}")
    print(
        f"  extrapolated to the {ORS_SPACING_M:.0f} m spacing ORS returns: {predicted:.0f} m, "
        "against 6,400-8,800 m reported"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
