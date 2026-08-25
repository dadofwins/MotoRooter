"""Geodesic helpers. Distances are the basis of every surface/quota calculation."""

import pytest

from motorooter.routing.geo import haversine_m, path_length_m
from motorooter.routing.models import Coordinate


def test_haversine_zero_for_identical_points():
    p = Coordinate(lat=45.0, lon=-121.0)
    assert haversine_m(p, p) == pytest.approx(0.0)


def test_haversine_one_degree_of_latitude_is_about_111km():
    a = Coordinate(lat=45.0, lon=-121.0)
    b = Coordinate(lat=46.0, lon=-121.0)
    assert haversine_m(a, b) == pytest.approx(111_195, rel=0.001)


def test_haversine_is_symmetric():
    a = Coordinate(lat=45.0, lon=-121.0)
    b = Coordinate(lat=45.5, lon=-120.2)
    assert haversine_m(a, b) == pytest.approx(haversine_m(b, a))


def test_haversine_handles_antimeridian_crossing():
    """Naive lon subtraction reports ~360 degrees here instead of ~2."""
    a = Coordinate(lat=0.0, lon=179.0)
    b = Coordinate(lat=0.0, lon=-179.0)
    assert haversine_m(a, b) == pytest.approx(222_390, rel=0.001)


def test_path_length_sums_consecutive_segments():
    pts = [
        Coordinate(lat=45.0, lon=-121.0),
        Coordinate(lat=46.0, lon=-121.0),
        Coordinate(lat=47.0, lon=-121.0),
    ]
    assert path_length_m(pts) == pytest.approx(222_390, rel=0.001)


@pytest.mark.parametrize("pts", [[], [Coordinate(lat=1.0, lon=1.0)]])
def test_path_length_of_degenerate_path_is_zero(pts):
    assert path_length_m(pts) == 0.0
