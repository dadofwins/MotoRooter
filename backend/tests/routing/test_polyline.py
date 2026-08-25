"""Google's encoded polyline format.

Decoded at the adapter boundary so encoded strings never enter domain models.
"""

import pytest

from motorooter.routing.models import Coordinate
from motorooter.routing.providers.polyline import decode_polyline, encode_polyline

GOOGLE_DOC_EXAMPLE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
"""Reference vector from Google's polyline algorithm documentation."""


def test_decodes_the_reference_vector():
    points = decode_polyline(GOOGLE_DOC_EXAMPLE)
    assert [(round(p.lat, 5), round(p.lon, 5)) for p in points] == [
        (38.5, -120.2),
        (40.7, -120.95),
        (43.252, -126.453),
    ]


def test_empty_string_decodes_to_no_points():
    assert decode_polyline("") == []


def test_single_point_roundtrips_to_five_decimal_precision():
    """The format quantizes to 1e-5 degrees; anything finer is lost by design."""
    points = decode_polyline("_p~iF~ps|U")
    assert (round(points[0].lat, 5), round(points[0].lon, 5)) == (38.5, -120.2)


def test_handles_negative_deltas():
    points = decode_polyline(GOOGLE_DOC_EXAMPLE)
    assert points[2].lon < points[1].lon


def test_truncated_payload_raises_rather_than_silently_dropping_a_point():
    with pytest.raises(ValueError, match="truncated"):
        decode_polyline("_p~iF~ps|U_ulL")


def test_rejects_out_of_range_result():
    """A corrupt payload must fail here, not place a waypoint off the globe."""
    with pytest.raises(ValueError):
        decode_polyline("__________")


class TestEncoding:
    def test_encodes_the_reference_vector(self):
        points = [
            Coordinate(lat=38.5, lon=-120.2),
            Coordinate(lat=40.7, lon=-120.95),
            Coordinate(lat=43.252, lon=-126.453),
        ]
        assert encode_polyline(points) == GOOGLE_DOC_EXAMPLE

    def test_empty_input_encodes_to_empty_string(self):
        assert encode_polyline([]) == ""

    def test_roundtrips_within_quantization_error(self):
        points = [Coordinate(lat=45.5152, lon=-122.6784), Coordinate(lat=45.3311, lon=-121.7113)]
        decoded = decode_polyline(encode_polyline(points))
        for original, result in zip(points, decoded, strict=True):
            assert result.lat == pytest.approx(original.lat, abs=1e-5)
            assert result.lon == pytest.approx(original.lon, abs=1e-5)
