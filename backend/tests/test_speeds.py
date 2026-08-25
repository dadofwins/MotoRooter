"""Riding speeds, and the duration derived from them.

The provider's own duration is unusable: hosted ORS's `cycling-mountain` profile returned
8.0 h for 133 km — bicycle times — and trip planning is organised around duration, so a
rider would be told a four-hour day takes eight.

The values here are guesses and are marked as such. They deserve the measurement the routing
profile just got; what they must not do is sit inline at a call site where nobody can find
them to correct.
"""

import pytest

from motorooter.routing.models import Surface
from motorooter.speeds import DEFAULT_RIDING_SPEEDS, RidingSpeeds


class TestTheSpeedTable:
    def test_dirt_is_slower_than_pavement(self):
        speeds = DEFAULT_RIDING_SPEEDS
        assert speeds.for_surface(Surface.UNPAVED) < speeds.for_surface(Surface.PAVED)

    def test_unknown_sits_between_the_two(self):
        """Absence of data is not evidence of dirt, nor of tarmac. Assume mixed."""
        speeds = DEFAULT_RIDING_SPEEDS
        assert (
            speeds.for_surface(Surface.UNPAVED)
            <= speeds.for_surface(Surface.UNKNOWN)
            <= speeds.for_surface(Surface.PAVED)
        )

    def test_every_surface_has_a_speed(self):
        """A new Surface member with no speed would silently divide by zero or KeyError."""
        for surface in Surface:
            assert DEFAULT_RIDING_SPEEDS.for_surface(surface) > 0

    def test_speeds_are_overridable_without_touching_code(self):
        slow = RidingSpeeds(paved_kmh=50.0, unpaved_kmh=25.0, unknown_kmh=35.0)
        assert slow.for_surface(Surface.PAVED) == 50.0

    def test_a_zero_speed_is_rejected(self):
        """Would make a leg take forever rather than fail visibly."""
        with pytest.raises(ValueError):
            RidingSpeeds(paved_kmh=0.0)

    def test_a_negative_speed_is_rejected(self):
        with pytest.raises(ValueError):
            RidingSpeeds(unpaved_kmh=-10.0)


class TestDurationFromDistance:
    def test_paved_distance_uses_the_paved_speed(self):
        speeds = RidingSpeeds(paved_kmh=80.0)
        assert speeds.seconds_for(80_000.0, Surface.PAVED) == pytest.approx(3600.0)

    def test_unpaved_distance_takes_longer_over_the_same_ground(self):
        speeds = RidingSpeeds(paved_kmh=80.0, unpaved_kmh=40.0)
        paved = speeds.seconds_for(40_000.0, Surface.PAVED)
        unpaved = speeds.seconds_for(40_000.0, Surface.UNPAVED)
        assert unpaved == pytest.approx(paved * 2)

    def test_zero_distance_takes_no_time(self):
        assert DEFAULT_RIDING_SPEEDS.seconds_for(0.0, Surface.PAVED) == 0.0


def test_the_defaults_are_marked_provisional():
    """They are invented. The docstring is the only thing stopping them being taken as fact."""
    assert "provisional" in (RidingSpeeds.__doc__ or "").lower()
