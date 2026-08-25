"""Decoder for Google's encoded polyline algorithm.

Values are stored as zig-zag encoded deltas in base64-ish 5-bit chunks, quantized to 1e-5
degrees. Kept in the providers package because it is a wire-format concern: callers only
ever see `Coordinate`.
"""

from collections.abc import Sequence

from pydantic import ValidationError

from motorooter.routing.models import Coordinate

_CHUNK_BITS = 5
_CHUNK_MASK = 0x1F
_CONTINUATION_BIT = 0x20
_ASCII_OFFSET = 63
_SCALE = 1e5


def _decode_value(encoded: str, index: int) -> tuple[int, int]:
    """Decode one zig-zag varint. Returns (value, next_index)."""
    result = 0
    shift = 0
    while True:
        if index >= len(encoded):
            msg = "truncated polyline: value ended mid-sequence"
            raise ValueError(msg)
        chunk = ord(encoded[index]) - _ASCII_OFFSET
        index += 1
        result |= (chunk & _CHUNK_MASK) << shift
        shift += _CHUNK_BITS
        if not chunk & _CONTINUATION_BIT:
            break
    # Low bit set means the original value was negative (zig-zag encoding).
    return (~(result >> 1) if result & 1 else result >> 1), index


def _encode_value(value: int) -> str:
    """Zig-zag encode one signed integer into 5-bit chunks."""
    zigzag = ~(value << 1) if value < 0 else value << 1
    chunks = []
    while zigzag >= _CONTINUATION_BIT:
        chunks.append(chr((_CONTINUATION_BIT | (zigzag & _CHUNK_MASK)) + _ASCII_OFFSET))
        zigzag >>= _CHUNK_BITS
    chunks.append(chr(zigzag + _ASCII_OFFSET))
    return "".join(chunks)


def encode_polyline(points: Sequence[Coordinate]) -> str:
    """Encode points to Google's polyline format.

    Lossy: coordinates are quantized to 1e-5 degrees (~1 m). Used for compact route
    storage and for building test fixtures that mirror real responses.
    """
    encoded = []
    prev_lat = prev_lon = 0
    for point in points:
        lat = round(point.lat * _SCALE)
        lon = round(point.lon * _SCALE)
        encoded.append(_encode_value(lat - prev_lat))
        encoded.append(_encode_value(lon - prev_lon))
        prev_lat, prev_lon = lat, lon
    return "".join(encoded)


def decode_polyline(encoded: str) -> list[Coordinate]:
    """Decode to WGS84 points.

    Raises:
        ValueError: payload is truncated or yields out-of-range coordinates.
    """
    points: list[Coordinate] = []
    index = 0
    lat = lon = 0
    while index < len(encoded):
        dlat, index = _decode_value(encoded, index)
        dlon, index = _decode_value(encoded, index)
        lat += dlat
        lon += dlon
        try:
            points.append(Coordinate(lat=lat / _SCALE, lon=lon / _SCALE))
        except ValidationError as exc:
            msg = f"decoded coordinate out of range at point {len(points)}"
            raise ValueError(msg) from exc
    return points
