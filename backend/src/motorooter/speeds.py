"""How fast a motorcycle covers ground, by surface.

Exists because the routing provider's own duration cannot be used. Hosted ORS has no
motorcycle profile, so dirt-capable legs route through `cycling-mountain`, which returns
*bicycle* times — 8.0 hours for 133 km, about 16 km/h, measured against a real WABDR section.
Trip planning is organised around duration ("a five-day trip"), so passing that figure
through would tell a rider a four-hour day takes eight and make any day-splitting nonsense.

`RouteLeg.duration_s` still reports what the provider said, because that is what the provider
said. Nothing user-facing reads it.

At the package root rather than under `planning/` so `trips.models` can use it: `planning`
imports `trips`, and the reverse would be a cycle.
"""

import dataclasses

from motorooter.routing.models import RouteLeg, Surface

_SECONDS_PER_HOUR = 3600.0
_METRES_PER_KM = 1000.0


@dataclasses.dataclass(frozen=True)
class RidingSpeeds:
    """Average speeds by surface, in km/h.

    **These values are provisional.** They are informed guesses, not measurements, and they
    deserve the treatment the routing-profile question got: a real ride compared against a
    real clock. They live here, named and in one place, so that when someone has that data
    the correction is a one-line change rather than a hunt through call sites.

    Averages over a riding day, not top speeds — they absorb fuel stops, photographs, gates,
    and the fact that nobody maintains 80 km/h for six hours.
    """

    paved_kmh: float = 80.0
    unpaved_kmh: float = 40.0

    unknown_kmh: float = 55.0
    """Between the two, deliberately.

    Absence of surface data is not evidence of tarmac, and treating it as either extreme
    biases every estimate on roads nobody has tagged — which, on the back roads this app
    exists to find, is most of them.
    """

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if value <= 0:
                # A zero would divide to infinity and read as "this leg never ends"; a
                # negative would silently shorten the trip. Both should fail at the source.
                msg = f"{field.name} must be positive, got {value}"
                raise ValueError(msg)

    def for_surface(self, surface: Surface) -> float:
        """Speed in km/h. Every `Surface` member has one; a new member must add one here."""
        match surface:
            case Surface.PAVED:
                return self.paved_kmh
            case Surface.UNPAVED:
                return self.unpaved_kmh
            case Surface.UNKNOWN:
                return self.unknown_kmh

    def seconds_for(self, metres: float, surface: Surface) -> float:
        """Time to cover `metres` of `surface`."""
        km = metres / _METRES_PER_KM
        return km / self.for_surface(surface) * _SECONDS_PER_HOUR


DEFAULT_RIDING_SPEEDS = RidingSpeeds()
"""The table in use. Provisional — see `RidingSpeeds`."""


def estimate_leg_duration_s(leg: RouteLeg, speeds: RidingSpeeds = DEFAULT_RIDING_SPEEDS) -> float:
    """Riding time for one leg, from its distance and surface mix.

    Lives here rather than on `RouteLeg` because the dependency only runs one way: this
    module imports `routing.models` for `Surface`, so a `RouteLeg` reaching back for the
    speed table would close a cycle. It is also the honest boundary — the domain model
    should keep reporting what the provider said, and the layers above should say what we
    believe instead.
    """
    return (
        speeds.seconds_for(leg.paved_distance_m, Surface.PAVED)
        + speeds.seconds_for(leg.unpaved_distance_m, Surface.UNPAVED)
        + speeds.seconds_for(leg.unknown_distance_m, Surface.UNKNOWN)
    )
