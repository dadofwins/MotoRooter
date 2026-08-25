"""Twistiness against real recorded geometry.

The unit tests in `test_metrics.py` use hand-built polylines, which have no sampling noise
and therefore pass any threshold — including the one that scored a dead-straight road at a
third of a right-angle bend per kilometre. These fixtures are real ORS output, recorded once
by `scripts/record_geometry_fixtures.py` and committed, so the metric is checked against the
shape of data it will actually see. No test here touches the network.

The property being pinned is *separation*. A twistiness number means nothing on its own; what
matters is that the roads a rider wants score far above the roads they do not, and that the
ordering survives someone tidying up the arithmetic later.

**What these fixtures do not pin, and why.** They do not demonstrate that the 50 m threshold
is necessary. That justification rests on densely sampled geometry, where sub-threshold
wiggle accumulates into a fake corner — and this recorded interstate has a median segment of
64.6 m, with only 30% of segments below the threshold, so the filter barely engages on it.
Removing the threshold entirely leaves separation on *this* data at 10x rather than 7x.

That is a real limitation and it is recorded rather than papered over: the integrator's own
measurement found an interstate scoring 366 deg/km unfiltered, which needs geometry far
denser than anything recorded here. The synthetic jitter tests in `test_metrics.py` are what
currently pin the threshold's purpose; these pin the outcome.
"""

import json
import pathlib

import pytest

from motorooter.planning.metrics import detour_ratio, twistiness_deg_per_km
from motorooter.routing.models import Coordinate

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "geometry"

_UNPAVED_SURFACE_CODES = {2, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18}
"""ORS surface codes meaning "not sealed".

Mirrors the adapter's table in `routing/providers/ors.py`, because the fixture holds raw ORS
codes rather than decoded spans.
"""


def road(name: str) -> tuple[Coordinate, ...]:
    document = json.loads((FIXTURES / f"{name}.json").read_text())
    return tuple(Coordinate(lat=lat, lon=lon) for lat, lon in document["geometry"])


def unpaved_fraction(name: str) -> float:
    document = json.loads((FIXTURES / f"{name}.json").read_text())
    points = len(document["geometry"])
    unpaved = sum(
        end - start
        for start, end, code in document["surface_values"]
        if code in _UNPAVED_SURFACE_CODES
    )
    return float(unpaved) / max(points - 1, 1)


@pytest.fixture(scope="module")
def twistiness() -> dict[str, float]:
    return {
        name: twistiness_deg_per_km(road(name))
        for name in ("wabdr-3", "i90", "twisty-paved", "twisty-paved-alt")
    }


class TestItSeparatesTheRoadsRidersCareAbout:
    def test_dirt_scores_far_above_an_interstate(self, twistiness):
        """The headline. A metric that cannot tell I-90 from the WABDR is not a metric."""
        assert twistiness["wabdr-3"] / twistiness["i90"] > 4.0

    def test_a_twisty_paved_pass_scores_far_above_an_interstate(self, twistiness):
        """The case that had not been measured, and the one the scorer depends on.

        If a great paved road did not separate from a motorway, then twistiness plus surface
        could not express "great motorcycle road" and the scorer would need another signal.
        Chinook Pass comes in at roughly 3x the interstate.
        """
        assert twistiness["twisty-paved"] / twistiness["i90"] > 2.0

    def test_two_different_twisty_paved_roads_agree(self, twistiness):
        """Chinook Pass and Chuckanut Drive are unrelated roads of the same character."""
        assert twistiness["twisty-paved"] == pytest.approx(twistiness["twisty-paved-alt"], rel=0.35)

    def test_the_ordering_is_interstate_then_paved_pass_then_dirt(self, twistiness):
        assert twistiness["i90"] < twistiness["twisty-paved-alt"] < twistiness["wabdr-3"]

    def test_an_interstate_is_not_merely_less_twisty_but_barely_twisty(self, twistiness):
        """A ratio can be satisfied by inflating both sides; this pins the absolute floor."""
        assert twistiness["i90"] < 80.0

    def test_a_mountain_dirt_section_is_absolutely_twisty(self, twistiness):
        assert twistiness["wabdr-3"] > 150.0


class TestSurfaceIsTheOtherHalfOfTheAnswer:
    """Twistiness alone ranks dirt above a great paved road; surface is what tells them apart."""

    def test_the_paved_pass_reports_no_dirt(self):
        assert unpaved_fraction("twisty-paved") == 0.0

    def test_the_bdr_section_reports_substantial_dirt(self):
        assert unpaved_fraction("wabdr-3") > 0.2

    def test_together_they_distinguish_three_kinds_of_road(self, twistiness):
        """Which is the whole basis of scoring: twisty-and-sealed is not twisty-and-gravel,
        and neither is a motorway."""
        assert twistiness["i90"] < 80.0 and unpaved_fraction("i90") == 0.0
        assert twistiness["twisty-paved"] > 80.0 and unpaved_fraction("twisty-paved") == 0.0
        assert twistiness["wabdr-3"] > 80.0 and unpaved_fraction("wabdr-3") > 0.2


class TestDetourRatioOnRealRoads:
    def test_an_interstate_goes_more_directly_than_a_pass(self):
        """It is built to; that is what makes it dull and what makes the ratio informative."""
        assert detour_ratio(road("i90")) < detour_ratio(road("twisty-paved"))

    def test_every_real_road_is_at_least_as_long_as_the_direct_line(self):
        for name in ("wabdr-3", "i90", "twisty-paved", "twisty-paved-alt"):
            assert detour_ratio(road(name)) >= 1.0


def test_the_fixtures_are_the_density_the_scorer_will_see():
    """ORS output, not a GPS recording. The distinction decides whether noise exists at all,
    and it is the reason this file cannot pin the threshold — see the module docstring."""
    document = json.loads((FIXTURES / "wabdr-3.json").read_text())
    spacing = document["distance_m"] / (len(document["geometry"]) - 1)
    assert spacing < 60.0
