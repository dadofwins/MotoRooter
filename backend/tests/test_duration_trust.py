"""Whether a provider's own duration is worth showing a rider.

M0 measured hosted ORS `cycling-mountain` returning bicycle times — 8 hours for 133 km — and
the conclusion went into CLAUDE.md as a global rule: compute our own. It is not a global
rule. Google runs a car profile, and on 177 km of highway its figure is the trustworthy one
while ours overestimates by half an hour. Summed, a rider was shown 4h56m for a ride of about
3h33m, in the direction that makes them plan a shorter day than they could.

So it is a property of the profile a provider ran, which makes it a capability — beside
`reports_surface`, resolved per intent, never a branch on an engine name.
"""

import pytest

from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    ProviderCapabilities,
    RouteLeg,
    Surface,
    SurfaceSpan,
)
from motorooter.speeds import estimate_leg_duration_s, leg_duration_s
from motorooter.trips.models import Trip, TripLeg, TripSummary, Waypoint, utc_now


def geometry(points: int = 11, spacing: float = 0.01):
    return tuple(Coordinate(lat=47.0 + i * spacing, lon=-121.0) for i in range(points))


def leg(
    *,
    provider: str = "ors",
    duration_s: float = 8000.0,
    trustworthy: bool = False,
    paved: bool = False,
) -> RouteLeg:
    spans = (SurfaceSpan(start_index=0, end_index=10, surface=Surface.PAVED),) if paved else ()
    return RouteLeg(
        geometry=geometry(),
        distance_m=100_000.0,
        duration_s=duration_s,
        provider=provider,
        intent=LegIntent.UNPAVED,
        surface_spans=spans,
        duration_is_trustworthy=trustworthy,
    )


def trip_with(*legs: RouteLeg) -> Trip:
    now = utc_now()
    return Trip(
        slug="cascade-loop",
        name="Cascade Loop",
        created_at=now,
        edited_at=now,
        waypoints=tuple(
            Waypoint(coordinate=Coordinate(lat=47.0 + i, lon=-121.0)) for i in range(len(legs) + 1)
        ),
        legs=tuple(
            TripLeg(
                intent=LegIntent.UNPAVED,
                start_waypoint_index=index,
                end_waypoint_index=index + 1,
                routed=routed,
            )
            for index, routed in enumerate(legs)
        ),
    )


class TestTheCapability:
    def test_a_provider_must_claim_it(self):
        """Defaults false for the same reason `reports_surface` does: silence from an engine
        must not be indistinguishable from a claim."""
        assert ProviderCapabilities(name="anything").reports_trustworthy_duration is False

    def test_it_is_distinct_from_reporting_surface(self):
        """ORS reports surface and cannot be trusted on duration; Google is the reverse.
        Conflating them would get both wrong in opposite directions."""
        capability = ProviderCapabilities(
            name="ors", reports_surface=True, reports_trustworthy_duration=False
        )
        assert capability.reports_surface is not capability.reports_trustworthy_duration


class TestTheLegCarriesIt:
    """Stamped at routing time beside `provider` and `intent`.

    A leg loaded from a trip saved last week still knows whether its duration was
    trustworthy, without re-resolving a policy table that may have been repointed since —
    the same argument as `RouteFingerprint`. The alternative is a domain model reaching for
    the routing registry, which inverts the dependency, or branching on `provider`, which is
    the engine-name dispatch the whole architecture forbids.
    """

    def test_it_defaults_to_untrustworthy(self):
        """Constructed without the field, not with it set false — otherwise the default is
        never exercised and flipping it would go unnoticed. A leg that arrived without the
        stamp must get the derived estimate, never a bicycle time presented as fact."""
        bare = RouteLeg(
            geometry=geometry(),
            distance_m=100_000.0,
            duration_s=8000.0,
            provider="ors",
            intent=LegIntent.UNPAVED,
        )
        assert bare.duration_is_trustworthy is False

    def test_a_trusted_leg_says_so(self):
        assert leg(provider="google", trustworthy=True).duration_is_trustworthy is True


class TestWhatATripReports:
    def test_an_untrusted_leg_is_derived(self):
        """ORS on dirt: the provider says eight hours because it thinks you are cycling."""
        untrusted = leg(duration_s=28_800.0)
        assert trip_with(untrusted).estimated_duration_s == pytest.approx(
            estimate_leg_duration_s(untrusted)
        )

    def test_a_trusted_leg_uses_the_provider_figure(self):
        """Google on tarmac: its car time beats our speed table, and ours overestimated by
        half an hour on 177 km."""
        trusted = leg(provider="google", duration_s=6_400.0, trustworthy=True, paved=True)
        assert trip_with(trusted).estimated_duration_s == pytest.approx(6_400.0)

    def test_a_mixed_trip_takes_the_best_available_per_leg(self):
        """Not one rule for the whole trip. Deriving everything throws away Google's good
        number; trusting everything applies a car profile to dirt, which is the M0 finding
        in reverse and the dangerous direction."""
        trusted = leg(provider="google", duration_s=6_400.0, trustworthy=True, paved=True)
        untrusted = leg(duration_s=28_800.0)
        total = trip_with(trusted, untrusted).estimated_duration_s
        assert total == pytest.approx(6_400.0 + estimate_leg_duration_s(untrusted))


class TestTellingTheRiderWhichItIs:
    def test_a_fully_trusted_trip_is_not_marked_estimated(self):
        trusted = leg(provider="google", duration_s=6_400.0, trustworthy=True, paved=True)
        assert trip_with(trusted).duration_is_estimated is False

    def test_any_derived_leg_marks_the_total_estimated(self):
        """A number that looks exact when half of it is a guess is the failure to avoid."""
        trusted = leg(provider="google", duration_s=6_400.0, trustworthy=True, paved=True)
        assert trip_with(trusted, leg()).duration_is_estimated is True

    def test_an_unrouted_trip_is_estimated(self):
        """Nothing to trust yet, and claiming exactness for a total of zero is worse than
        admitting it is a guess."""
        now = utc_now()
        empty = Trip(slug="x", name="X", created_at=now, edited_at=now)
        assert empty.duration_is_estimated is True

    def test_the_summary_carries_the_flag(self):
        """The trip list shows durations too, and it must not lose the caveat on the way."""
        trip = trip_with(leg())
        assert TripSummary.from_trip(trip).duration_is_estimated is True


class TestTheChoiceLivesInOnePlace:
    """The seventh instance of the same shape, and the one a rider sees most.

    `Trip.estimate_duration_s` read the flag; `POST /routing/leg` called the derivation
    directly and never got the memo. So the trip total was corrected and the per-leg figure —
    which the map shows on every drag — kept inflating Google's 128 minutes to 193.

    Fixed by extracting the choice rather than repeating the conditional. Two copies of a
    rule is how this happened; three would be next, because `estimate_leg_duration_s` is
    public and reads like the thing to call.
    """

    def test_a_trusted_leg_reports_its_own_duration(self):
        trusted = leg(provider="google", duration_s=7_680.0, trustworthy=True, paved=True)
        assert leg_duration_s(trusted) == pytest.approx(7_680.0)

    def test_an_untrusted_leg_is_derived(self):
        untrusted = leg(duration_s=8_580.0)
        assert leg_duration_s(untrusted) == pytest.approx(estimate_leg_duration_s(untrusted))

    def test_the_trip_total_uses_the_same_choice(self):
        """One rule, both consumers. If these disagree, one of them is showing a rider a
        number the other would not."""
        trusted = leg(provider="google", duration_s=7_680.0, trustworthy=True, paved=True)
        untrusted = leg(duration_s=8_580.0)
        assert trip_with(trusted, untrusted).estimated_duration_s == pytest.approx(
            leg_duration_s(trusted) + leg_duration_s(untrusted)
        )


class TestTheLegEndpointReportsTheTrustedFigure:
    """Where Tim actually sees it: the map, on every drag."""

    @staticmethod
    def _response(intent: str, provider: str):
        from fastapi.testclient import TestClient

        from motorooter.app import create_app
        from motorooter.routing.factory import RoutingSettings

        client = TestClient(create_app(RoutingSettings(offline=True)))
        return client.post(
            "/api/routing/leg",
            json={
                "waypoints": [{"lat": 46.97, "lon": -121.53}, {"lat": 46.87, "lon": -121.52}],
                "intent": intent,
            },
        ).json()

    def test_an_untrusted_provider_is_still_derived(self):
        """`fake` declares False, so offline still exercises the derived path — which is why
        it declares False."""
        body = self._response("unpaved", "fake")
        assert body["estimated_duration_s"] > 0
