"""Geodesic helpers.

Great-circle distance is accurate enough for surface accounting and cache-key rounding;
we deliberately avoid a full geodesic (Vincenty/Karney) dependency for this.
"""

from __future__ import annotations

from itertools import pairwise
from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from motorooter.routing.models import Coordinate

# Imported under TYPE_CHECKING only: models imports this module, and only .lat/.lon are
# touched at runtime, so keeping the dependency type-only breaks the import cycle.

EARTH_RADIUS_M = 6_371_008.8
"""IUGG mean Earth radius."""


def haversine_m(a: Coordinate, b: Coordinate) -> float:
    """Great-circle distance in metres.

    Uses the haversine form, which stays numerically stable for the short hops that
    dominate route geometry (a spherical law-of-cosines form loses precision there).
    """
    lat1, lat2 = radians(a.lat), radians(b.lat)
    dlat = lat2 - lat1
    dlon = radians(b.lon - a.lon)
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(h))


def path_length_m(points: Sequence[Coordinate]) -> float:
    """Summed great-circle length of a polyline. Degenerate paths measure 0.0."""
    return sum(haversine_m(a, b) for a, b in pairwise(points))
