"""What a routed leg was routed *from*.

Staleness was previously approximated by comparing a cached leg's intent and provider
against what its trip now asks for. That catches a retag, and cannot catch a moved
waypoint: engines snap waypoints to the nearest routable node, sometimes by hundreds of
metres, so no endpoint tolerance separates "the engine snapped this" from "the user dragged
this". It is not a threshold that needs tuning — it is a distinction the geometry cannot
express.

Recording the request makes it decidable instead. The rounding matches the routing cache's
key precision, so two requests the cache would serve from one entry also fingerprint
identically; anything else would report a leg as stale that the cache had just declared
interchangeable.
"""

import pytest

from motorooter.routing.decorators.caching import CachingProvider
from motorooter.routing.models import (
    COORDINATE_KEY_PRECISION,
    Coordinate,
    LegIntent,
    RouteFingerprint,
    RouteRequest,
)
from motorooter.routing.providers.fake import FakeProvider


def request(
    *coords: tuple[float, float],
    intent: LegIntent = LegIntent.UNPAVED,
) -> RouteRequest:
    return RouteRequest(
        waypoints=tuple(Coordinate(lat=lat, lon=lon) for lat, lon in coords),
        intent=intent,
    )


class TestCapturingARequest:
    def test_records_the_waypoints(self):
        fingerprint = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        assert len(fingerprint.waypoints) == 2

    def test_records_the_intent(self):
        fingerprint = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        assert fingerprint.intent is LegIntent.UNPAVED

    def test_records_an_explicit_provider_override(self):
        fingerprint = RouteFingerprint.of(
            request((45.0, -121.0), (46.0, -121.0)), provider_override="ors"
        )
        assert fingerprint.provider_override == "ors"

    def test_no_override_is_recorded_as_none(self):
        assert (
            RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0))).provider_override is None
        )


class TestWhatCountsAsTheSameRequest:
    def test_an_identical_request_fingerprints_equal(self):
        first = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        second = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        assert first == second

    def test_jitter_below_the_key_precision_is_the_same_request(self):
        """Matches the cache: a difference it would serve from one entry is not a change."""
        nudge = 10 ** -(COORDINATE_KEY_PRECISION + 2)
        first = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        second = RouteFingerprint.of(request((45.0 + nudge, -121.0), (46.0, -121.0)))
        assert first == second

    def test_a_moved_waypoint_fingerprints_differently(self):
        """The case the intent-and-provider check could not see."""
        first = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        second = RouteFingerprint.of(request((45.0, -121.0), (46.5, -121.0)))
        assert first != second

    def test_an_added_via_point_fingerprints_differently(self):
        first = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        second = RouteFingerprint.of(request((45.0, -121.0), (45.5, -121.0), (46.0, -121.0)))
        assert first != second

    def test_reordered_waypoints_fingerprint_differently(self):
        """Same set, different route. Order is the whole point of a waypoint list."""
        first = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        second = RouteFingerprint.of(request((46.0, -121.0), (45.0, -121.0)))
        assert first != second

    def test_a_changed_intent_fingerprints_differently(self):
        first = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        second = RouteFingerprint.of(
            request((45.0, -121.0), (46.0, -121.0), intent=LegIntent.HIGHWAY_CONNECTOR)
        )
        assert first != second

    def test_a_changed_override_fingerprints_differently(self):
        base = request((45.0, -121.0), (46.0, -121.0))
        assert RouteFingerprint.of(base, provider_override="ors") != RouteFingerprint.of(
            base, provider_override="google"
        )

    def test_pinning_a_previously_unpinned_leg_fingerprints_differently(self):
        base = request((45.0, -121.0), (46.0, -121.0))
        assert RouteFingerprint.of(base) != RouteFingerprint.of(base, provider_override="ors")


class TestAgreementWithTheRoutingCache:
    """The fingerprint and the cache must consider the same requests interchangeable.

    If the fingerprint were stricter, a cache hit would be reported as stale geometry. If it
    were looser, a genuinely different request would reuse a leg the cache would have
    re-fetched.
    """

    @pytest.mark.parametrize(
        "second",
        [
            ((45.0, -121.0), (46.0, -121.0)),
            ((45.0 + 1e-9, -121.0), (46.0, -121.0)),
        ],
    )
    async def test_a_cache_hit_implies_an_equal_fingerprint(self, second):
        inner = FakeProvider()
        caching = CachingProvider(inner)
        first_request = request((45.0, -121.0), (46.0, -121.0))
        second_request = request(*second)

        await caching.route(first_request)
        await caching.route(second_request)

        assert caching.hits == 1
        assert RouteFingerprint.of(first_request) == RouteFingerprint.of(second_request)

    async def test_a_cache_miss_implies_a_different_fingerprint(self):
        inner = FakeProvider()
        caching = CachingProvider(inner)
        first_request = request((45.0, -121.0), (46.0, -121.0))
        second_request = request((45.0, -121.0), (46.5, -121.0))

        await caching.route(first_request)
        await caching.route(second_request)

        assert caching.misses == 2
        assert RouteFingerprint.of(first_request) != RouteFingerprint.of(second_request)


class TestItStaysReadable:
    def test_it_stores_coordinates_rather_than_a_hash(self):
        """A hash is smaller and enough for equality, and tells you nothing when a rider
        reports that their route is wrongly marked stale. The values name the field."""
        fingerprint = RouteFingerprint.of(request((45.0, -121.0), (46.0, -121.0)))
        assert fingerprint.waypoints[0].lat == pytest.approx(45.0)

    def test_it_survives_a_json_round_trip(self):
        original = RouteFingerprint.of(
            request((45.123456, -121.7), (46.0, -121.0)), provider_override="ors"
        )
        assert RouteFingerprint.model_validate_json(original.model_dump_json()) == original
