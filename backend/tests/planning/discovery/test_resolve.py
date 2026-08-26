"""Turning a claimed name into a real place, or dropping it.

This is where a claim becomes a fact. Everything upstream is untrusted: a web page said a
campsite exists, a model read the name out of the prose. Places is what decides whether the
thing is real, where it actually is, and therefore whether it may be shown to a rider at all.

Two rules the stage enforces rather than assumes.

**A candidate that will not resolve is dropped, never guessed at.** The `Poi` model already
refuses to pin an unverified suggestion, so the invariant holds either way — but discarding
is the right behaviour, not pinning something plausible at a made-up coordinate.

**Distance is the relevance filter.** The extract stage cannot do it: on the Chinook Pass
corridor it returned Miller Peak and Stafford Creek, both real, both correctly identified as
Washington, and both about 100 km away in the Teanaway. "In the right state" is the finest
judgement available from text. A corridor is tens of metres wide, so the filter has to be
arithmetic, and arithmetic needs the coordinate that only this stage produces.
"""

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest
import respx

from motorooter.planning.discovery.errors import (
    DiscoveryError,
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.planning.discovery.models import Candidate
from motorooter.planning.discovery.resolve import (
    PLACES_SEARCH_URL,
    PlacesResolver,
)
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory

ANCHOR = Coordinate(lat=46.87, lon=-121.52)

# A short corridor running north from the anchor.
ROUTE = tuple(Coordinate(lat=46.87 + index * 0.001, lon=-121.52) for index in range(50))


def candidate(name: str = "Halfway Flat Campground") -> Candidate:
    return Candidate(
        name=name,
        category=PoiCategory.WILD_CAMP,
        found_near=ANCHOR,
        source="brave",
        snippet="dispersed sites below the summit",
    )


def place(
    *,
    place_id: str = "ChIJ_halfway",
    lat: float = 46.872,
    lon: float = -121.519,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": place_id,
        "displayName": {"text": "Halfway Flat Campground"},
        "location": {"latitude": lat, "longitude": lon},
        "types": ["campground", "point_of_interest"],
    } | extra


def body(*places: dict[str, Any]) -> dict[str, Any]:
    return {"places": list(places)}


@pytest.fixture
def mock_places():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(200, json=body(place())))
        yield mock


def resolver(**overrides: Any) -> PlacesResolver:
    return PlacesResolver(**({"api_key": "places-test-key"} | overrides))


class TestTheRequest:
    async def test_it_uses_the_new_places_endpoint(self, mock_places):
        await resolver().resolve([candidate()], route=ROUTE)
        assert str(mock_places.calls.last.request.url).startswith(PLACES_SEARCH_URL)

    async def test_the_key_goes_in_a_header_not_the_url(self, mock_places):
        """A key in a URL leaks into logs, referrers and error messages."""
        await resolver().resolve([candidate()], route=ROUTE)
        request = mock_places.calls.last.request
        assert request.headers["x-goog-api-key"] == "places-test-key"
        assert "places-test-key" not in str(request.url)

    async def test_a_field_mask_is_sent(self, mock_places):
        """Mandatory on this API, and it decides the billing tier."""
        await resolver().resolve([candidate()], route=ROUTE)
        assert mock_places.calls.last.request.headers["x-goog-fieldmask"]

    async def test_the_field_mask_requests_only_what_is_used(self, mock_places):
        """Ratings are in, photos and reviews are out.

        The mask sets the billing tier, so this is a cost decision as much as a data one. A
        rating is a fact about whether a place is worth stopping at and the judge should be
        handed it. Photos and reviews cannot be stored under Google's terms and nothing
        displays them, so requesting them would pay a higher tier for data thrown away.
        """
        await resolver().resolve([candidate()], route=ROUTE)
        mask = mock_places.calls.last.request.headers["x-goog-fieldmask"]
        assert "places.id" in mask
        assert "places.rating" in mask
        assert "photos" not in mask
        assert "reviews" not in mask

    async def test_a_rating_is_carried_through(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=body(place(rating=4.4, userRatingCount=15)))
            )
            resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].rating == pytest.approx(4.4)
        assert resolved[0].user_rating_count == 15

    async def test_a_missing_rating_is_absent_not_zero(self):
        """Unrated is not badly rated, and a zero would rank it below a one-star diner."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(200, json=body(place())))
            resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].rating is None

    @pytest.mark.parametrize("bogus", [7.0, -1.0, "great", True, None])
    async def test_an_impossible_rating_is_dropped_rather_than_clamped(self, bogus):
        """A 7-star rating means the field is not what we think it is; inventing a 5 hides
        that, and a clamped value is indistinguishable from a real one."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=body(place(rating=bogus)))
            )
            resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].rating is None

    async def test_the_candidate_name_is_the_query(self, mock_places):
        await resolver().resolve([candidate("Road to Snag Lake")], route=ROUTE)
        assert "Snag Lake" in mock_places.calls.last.request.read().decode()

    async def test_the_search_is_biased_to_the_anchor(self, mock_places):
        """Names repeat across a continent; the corridor is the disambiguator.

        Parsed rather than substring-matched: `"locationBias" in body` also matches
        `_locationBias`, so a typo'd key would have passed a text search of the payload.
        """
        await resolver().resolve([candidate()], route=ROUTE)
        sent = json.loads(mock_places.calls.last.request.read())
        centre = sent["locationBias"]["circle"]["center"]
        assert centre["latitude"] == pytest.approx(ANCHOR.lat)
        assert centre["longitude"] == pytest.approx(ANCHOR.lon)


class TestWhatResolutionProduces:
    async def test_it_carries_the_place_id(self, mock_places):
        resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].place_id == "ChIJ_halfway"

    async def test_the_coordinate_comes_from_places(self, mock_places):
        resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].coordinate.lat == pytest.approx(46.872)

    async def test_it_keeps_the_original_claim(self, mock_places):
        """Provenance: which source suggested it, and on what evidence."""
        resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].candidate.snippet == "dispersed sites below the summit"

    async def test_the_result_can_be_pinned(self, mock_places):
        resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].to_poi(poi_id="p1", on_route=True).is_verified is True

    async def test_the_category_comes_from_places_not_the_query(self):
        """`Crystal Mountain Resort` arrived tagged `wild_camp` because it turned up in a
        dispersed-camping search. It is a ski resort with lodging."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=body(place(types=["ski_resort", "lodging"])))
            )
            resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert candidate().category is PoiCategory.WILD_CAMP
        assert resolved[0].category is PoiCategory.HOTEL

    async def test_places_types_are_kept_as_evidence(self, mock_places):
        """The model needs them when it has to decide what Places could not."""
        resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert "campground" in resolved[0].places_types

    async def test_an_untypeable_place_has_no_category_rather_than_the_querys(self):
        """Dispersed camping has no Google type. Falling back to the query is the bug."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(
                    200, json=body(place(types=["point_of_interest", "establishment"]))
                )
            )
            resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].category is None

    async def test_the_field_mask_requests_types(self, mock_places):
        await resolver().resolve([candidate()], route=ROUTE)
        assert "places.types" in mock_places.calls.last.request.headers["x-goog-fieldmask"]


class TestUnresolvableCandidatesAreDropped:
    async def test_no_match_yields_nothing(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(200, json={}))
            assert await resolver().resolve([candidate()], route=ROUTE) == ()

    async def test_an_empty_places_list_yields_nothing(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(200, json=body()))
            assert await resolver().resolve([candidate()], route=ROUTE) == ()

    async def test_a_place_without_an_id_is_dropped(self):
        """It cannot be persisted or re-fetched, so it is not resolved."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(
                    200, json={"places": [{"location": {"latitude": 46.87, "longitude": -121.5}}]}
                )
            )
            assert await resolver().resolve([candidate()], route=ROUTE) == ()

    async def test_a_place_without_a_location_is_dropped(self):
        """Nothing goes on a map without a coordinate from Places."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"places": [{"id": "ChIJ_x"}]})
            )
            assert await resolver().resolve([candidate()], route=ROUTE) == ()

    async def test_one_failure_does_not_lose_the_others(self, mock_places):
        """A batch is several metered lookups; one miss must not discard the rest."""
        resolved = await resolver().resolve([candidate("A"), candidate("B")], route=ROUTE)
        assert len(resolved) >= 1


class TestDistanceIsTheRelevanceFilter:
    """The finding from the extract spike, turned into arithmetic."""

    async def test_a_place_far_from_the_route_is_dropped(self):
        """Miller Peak was real, correctly grounded, correctly Washington, and 100 km away."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=body(place(lat=47.8, lon=-120.9)))
            )
            assert await resolver().resolve([candidate()], route=ROUTE) == ()

    async def test_a_place_beside_the_route_is_kept(self, mock_places):
        assert len(await resolver().resolve([candidate()], route=ROUTE)) == 1

    async def test_the_corridor_width_is_configurable(self):
        """It is a guess like every other threshold here, so it is an argument."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=body(place(lat=46.95, lon=-121.52)))
            )
            near = await resolver().resolve([candidate()], route=ROUTE, corridor_m=50_000)
            far = await resolver().resolve([candidate()], route=ROUTE, corridor_m=100)
        assert len(near) == 1
        assert far == ()

    async def test_the_measured_distance_is_reported(self, mock_places):
        """It is evidence the judge needs, so it is computed once here rather than twice."""
        resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].distance_off_route_m is not None

    async def test_with_no_route_nothing_is_distance_filtered(self, mock_places):
        """Resolving without a corridor is legitimate — a single lookup by name."""
        assert len(await resolver().resolve([candidate()], route=())) == 1


class TestFailureTranslation:
    @staticmethod
    async def _raises(status: int, expected: type[DiscoveryError]) -> None:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(status))
            with pytest.raises(expected):
                await resolver().resolve([candidate()], route=ROUTE)

    async def test_a_permission_error_is_its_own_thing(self):
        """The server key is currently the browser key. When that is referrer-restricted,
        server-side Places starts failing with something that looks nothing like a config
        problem — so it gets a distinct, well-worded error rather than a generic one."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(403))
            with pytest.raises(DiscoveryRefused, match="referrer"):
                await resolver().resolve([candidate()], route=ROUTE)

    async def test_rate_limiting_is_retryable(self):
        await self._raises(429, DiscoveryRateLimited)

    async def test_a_spent_budget_is_not(self):
        await self._raises(402, DiscoveryQuotaExceeded)

    async def test_an_upstream_fault_is_unavailability(self):
        await self._raises(503, DiscoveryUnavailable)

    async def test_a_transport_error_is_unavailability(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(side_effect=httpx.ConnectError("no route"))
            with pytest.raises(DiscoveryUnavailable):
                await resolver().resolve([candidate()], route=ROUTE)


MALFORMED: list[Any] = [
    {"places": None},
    {"places": "not-a-list"},
    {"places": [None]},
    {"places": ["a string"]},
    {"places": [{"id": "x", "location": "not-an-object"}]},
    {"places": [{"id": "x", "location": {"latitude": "north"}}]},
    {"places": [{"id": 12345, "location": {"latitude": 46.87, "longitude": -121.5}}]},
    [],
    "a bare string",
    None,
]


class TestNothingButADiscoveryErrorEscapes:
    @pytest.mark.parametrize("malformed", MALFORMED)
    async def test_a_malformed_body_never_raises_something_else(self, malformed):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(200, json=malformed))
            # Either it drops the candidate or it raises a translated error. Anything
            # else — a TypeError from dict access, a ValidationError from a bad
            # coordinate — propagates and fails, which is the invariant.
            with contextlib.suppress(DiscoveryError):
                await resolver().resolve([candidate()], route=ROUTE)


class TestNothingBeyondPlaceIdIsPersisted:
    """Google's terms permit storing `place_id` indefinitely and very little else."""

    async def test_a_rating_never_reaches_the_persisted_shape(self):
        """It is carried in memory for the judge. `to_poi` is the boundary that drops it,
        and `Poi` is the only thing ever written to storage."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=body(place(rating=4.4, userRatingCount=15)))
            )
            resolved = await resolver().resolve([candidate()], route=ROUTE)
        assert resolved[0].rating == pytest.approx(4.4)
        assert "rating" not in resolved[0].to_poi(poi_id="p1").model_dump()

    async def test_the_poi_it_produces_carries_only_the_place_id(self, mock_places):
        resolved = await resolver().resolve([candidate()], route=ROUTE)
        document = resolved[0].to_poi(poi_id="p1").model_dump()
        assert document["place_id"] == "ChIJ_halfway"
        assert "rating" not in document


class TestItResolvesConcurrently:
    """Resolution was the last sequential stretch in a pipeline built for speed.

    A corridor produces dozens of names and each is one metered Places lookup, so doing them
    one at a time put the slowest part of discovery back after the fast parts were fixed.
    Bounded, because Places rate-limits and a burst of thirty is what trips it.
    """

    @staticmethod
    def _tracking_mock(mock, *, delay: float = 0.01):
        """Records how many lookups are in flight at once."""
        state = {"now": 0, "peak": 0}

        async def respond(request):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(delay)
            state["now"] -= 1
            return httpx.Response(200, json=body(place()))

        mock.post(PLACES_SEARCH_URL).mock(side_effect=respond)
        return state

    async def test_lookups_overlap(self):
        with respx.mock(assert_all_called=False) as mock:
            state = self._tracking_mock(mock)
            await resolver().resolve([candidate() for _ in range(8)], concurrency=4)
        assert state["peak"] > 1, "resolution is still sequential"

    async def test_it_never_exceeds_the_bound(self):
        """Places rate-limits, and an unbounded burst is what trips it."""
        with respx.mock(assert_all_called=False) as mock:
            state = self._tracking_mock(mock)
            await resolver().resolve([candidate() for _ in range(20)], concurrency=3)
        assert state["peak"] <= 3

    async def test_results_keep_the_order_they_were_given_in(self):
        """Completion order is a race. Two runs over the same corridor returning the same
        places in a different order would show the rider a list that reshuffles itself."""
        names = [f"Place {index}" for index in range(6)]
        with respx.mock(assert_all_called=False) as mock:

            async def respond(request):
                asked = json.loads(request.content)["textQuery"]
                # Later names answer faster, so completion order reverses input order.
                await asyncio.sleep(0.001 * (6 - names.index(asked)))
                return httpx.Response(
                    200,
                    json=body(place(place_id=f"ChIJ_{asked}", displayName={"text": asked})),
                )

            mock.post(PLACES_SEARCH_URL).mock(side_effect=respond)
            resolved = await resolver().resolve(
                [candidate(name=name) for name in names], concurrency=6
            )
        assert [item.candidate.name for item in resolved] == names


class TestOneFailedLookupDoesNotDiscardTheRest:
    """The failure parallelism makes likely, and the worst shape a failure can take.

    Six concurrent lookups against a per-minute ceiling is exactly the burst that earns a
    429. If one raising discards the thirty-nine that resolved, the run still *succeeds* —
    it reports nothing found, and the rider sees an empty map with no error to explain it.

    Same principle as the stage-level handling: a failure costs those results, not the run.
    """

    @staticmethod
    def _one_bad_apple(mock):
        """The third lookup rate-limits; the rest are fine."""
        seen = {"n": 0}

        async def respond(request):
            seen["n"] += 1
            if seen["n"] == 3:
                return httpx.Response(429, json={"error": {"message": "slow down"}})
            asked = json.loads(request.content)["textQuery"]
            return httpx.Response(200, json=body(place(place_id=f"ChIJ_{asked}")))

        mock.post(PLACES_SEARCH_URL).mock(side_effect=respond)

    async def test_the_survivors_are_returned(self):
        with respx.mock(assert_all_called=False) as mock:
            self._one_bad_apple(mock)
            resolved = await resolver().resolve(
                [candidate(name=f"Place {i}") for i in range(6)], concurrency=1
            )
        assert len(resolved) == 5

    async def test_it_does_not_raise(self):
        """Raising is what discarded the batch: resolve() aborted and the caller's
        stage-level handler turned forty results into zero."""
        with respx.mock(assert_all_called=False) as mock:
            self._one_bad_apple(mock)
            await resolver().resolve(
                [candidate(name=f"Place {i}") for i in range(6)], concurrency=1
            )

    async def test_every_lookup_still_happens(self):
        """One failure must not cancel the lookups queued behind it."""
        with respx.mock(assert_all_called=False) as mock:
            self._one_bad_apple(mock)
            await resolver().resolve(
                [candidate(name=f"Place {i}") for i in range(6)], concurrency=2
            )
            assert len(mock.calls) == 6

    async def test_everything_failing_still_raises(self):
        """Partial failure degrades; total failure raises.

        Swallowing every failure would be the same silent-empty-map bug relocated: the
        caller could not tell "Places rate-limited us" from "this corridor has nothing on
        it", and only one of those is worth telling a rider. One survivor is enough to
        prefer the results, which is what the tests above cover.
        """
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PLACES_SEARCH_URL).mock(return_value=httpx.Response(429, json={}))
            with pytest.raises(DiscoveryRateLimited):
                await resolver().resolve([candidate(), candidate()])
