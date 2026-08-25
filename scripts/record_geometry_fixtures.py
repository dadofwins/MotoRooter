"""Record real ORS geometry so the twistiness metric has a regression test.

Run once, by hand, with a key. The fixtures it writes are committed, and the test that reads
them never touches the network — the house rule is recorded fixtures, not live calls at test
time.

Why real geometry rather than synthetic: the threshold in `planning/metrics.py` exists to
reject sampling noise, and synthetic geometry has none. A hand-built polyline will pass any
threshold, including the one that scored a dead-straight road at a third of a right-angle
bend per kilometre. Only a real trace can tell the metric is still working.

Three roads, chosen to span the axis the scorer has to discriminate on:

    wabdr-3        dirt mountain section — the thing riders want
    i90            interstate — the thing they do not
    twisty-paved   a pass road — high twistiness, low unpaved

The third is the case that matters most and the one nobody has measured. If a great paved
motorcycle road does not separate from gravel, then twistiness plus surface cannot express
"great motorcycle road", and the scorer needs a different signal.

    uv run --project backend python scripts/record_geometry_fixtures.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ORS_BASE = "https://api.openrouteservice.org"
OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "backend/tests/fixtures/geometry"

# (lon, lat) pairs, in ORS order. Endpoints sit on the road itself so the snap is unambiguous.
ROADS: dict[str, dict[str, object]] = {
    "wabdr-3": {
        "description": "WABDR Section 3, Ellensburg to Cashmere. Dirt mountain riding.",
        "profile": "cycling-mountain",
        "coordinates": [[-120.5385, 47.0043], [-120.4700, 47.2500], [-120.4650, 47.5220]],
    },
    "i90": {
        "description": "Interstate 90 across Snoqualmie Pass. The road nobody rides for fun.",
        "profile": "driving-car",
        "coordinates": [[-121.7870, 47.4950], [-120.9390, 47.1950]],
    },
    "twisty-paved": {
        "description": "Chinook Pass, SR-410. Paved, and one of the best roads in the state.",
        "profile": "driving-car",
        "coordinates": [[-121.5340, 46.9720], [-121.5165, 46.8722]],
    },
    "twisty-paved-alt": {
        "description": "Chuckanut Drive, SR-11. Twisty, paved, open year round.",
        "profile": "driving-car",
        "coordinates": [[-122.4870, 48.6560], [-122.4930, 48.5620], [-122.3980, 48.5010]],
    },
}


def fetch(profile: str, coordinates: list[list[float]], key: str) -> dict[str, object]:
    request = urllib.request.Request(  # noqa: S310 -- fixed https URL
        f"{ORS_BASE}/v2/directions/{profile}/geojson",
        data=json.dumps(
            {"coordinates": coordinates, "extra_info": ["surface"], "elevation": False}
        ).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        body: dict[str, object] = json.loads(response.read())
        return body


def main() -> int:
    key = os.environ.get("ORS_API_KEY")
    if not key:
        print("ORS_API_KEY is not set; source backend/.env first", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, road in ROADS.items():
        try:
            body = fetch(str(road["profile"]), road["coordinates"], key)  # type: ignore[arg-type]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            print(f"{name}: HTTP {exc.code} — {detail}", file=sys.stderr)
            continue

        feature = body["features"][0]  # type: ignore[index,call-overload]
        positions = feature["geometry"]["coordinates"]
        summary = feature["properties"]["summary"]
        extras = feature["properties"].get("extras", {}).get("surface", {}).get("values", [])

        fixture = {
            "name": name,
            "description": road["description"],
            "profile": road["profile"],
            "distance_m": summary["distance"],
            # [lat, lon] rather than ORS's [lon, lat]: the fixture should be in the order
            # the domain model uses, so a test cannot transpose it by accident.
            "geometry": [[position[1], position[0]] for position in positions],
            "surface_values": extras,
        }
        path = OUT_DIR / f"{name}.json"
        path.write_text(json.dumps(fixture, separators=(",", ":")))
        spacing = summary["distance"] / max(len(positions) - 1, 1)
        print(
            f"{name}: {len(positions)} points, {summary['distance'] / 1000:.1f} km, "
            f"~{spacing:.0f} m spacing -> {path.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
