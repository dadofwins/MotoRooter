"""The discovery domain, and the one invariant it exists to enforce.

Search results and model output are *claims*. A web page says a hot spring is at some
coordinate; a model will invent one outright and sound certain. The rule is that nothing
reaches the map unresolved — and the way to make a rule hold is to make the type system carry
it, not to rely on every future caller remembering.

So a `Candidate` cannot be pinned to a route, because it has no trustworthy location at all.
Only resolution against Places produces a coordinate, and it produces it from Places rather
than from whoever claimed it.
"""

import pytest
from pydantic import ValidationError

from motorooter.planning.discovery.models import (
    Candidate,
    Evidence,
    ResolvedCandidate,
    ScoredCandidate,
)
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory, PoiSource


def candidate(**overrides) -> Candidate:
    defaults = {
        "name": "Ridge Hot Springs",
        "category": PoiCategory.UNIQUE_STAY,
        "found_near": Coordinate(lat=47.0, lon=-121.0),
        "source": "brave",
    }
    return Candidate(**(defaults | overrides))


def resolved(**overrides) -> ResolvedCandidate:
    defaults = {
        "candidate": candidate(),
        "place_id": "ChIJ_example",
        "coordinate": Coordinate(lat=47.01, lon=-121.02),
        "category": PoiCategory.CAMPGROUND,
    }
    return ResolvedCandidate(**(defaults | overrides))


class TestACandidateIsAClaim:
    def test_it_records_where_the_search_was_anchored(self):
        """Ours, not the source's — the only trustworthy location a candidate has."""
        assert candidate().found_near.lat == 47.0

    def test_it_can_carry_a_claimed_coordinate(self):
        """Sources offer them and they are worth keeping as a resolution hint."""
        claimed = Coordinate(lat=47.5, lon=-121.5)
        assert candidate(claimed_coordinate=claimed).claimed_coordinate == claimed

    def test_a_claimed_coordinate_is_optional(self):
        assert candidate().claimed_coordinate is None

    def test_it_records_which_source_made_the_claim(self):
        """Provenance decides how much to trust it and where to go back for more."""
        assert candidate(source="llm").source == "llm"

    def test_it_keeps_the_snippet_that_justified_it(self):
        """The judge needs the ride report's words, not a summary of them."""
        text = "the gravel washes out after spring melt"
        assert candidate(snippet=text).snippet == text

    def test_it_has_no_place_id(self):
        """That is what resolution produces. A candidate carrying one would look verified."""
        assert not hasattr(candidate(), "place_id")

    def test_it_cannot_be_turned_into_a_pinned_poi_directly(self):
        """The path to the map runs through resolution; there is no shortcut."""
        assert not hasattr(candidate(), "to_poi")


class TestResolutionIsWhatMakesItReal:
    def test_it_carries_a_place_id(self):
        assert resolved().place_id == "ChIJ_example"

    def test_the_coordinate_comes_from_resolution_not_the_claim(self):
        """A source claiming the wrong valley must not move the pin there."""
        claimed = Coordinate(lat=40.0, lon=-100.0)
        result = resolved(candidate=candidate(claimed_coordinate=claimed))
        assert result.coordinate != claimed

    def test_an_empty_place_id_is_refused(self):
        """It would satisfy "has a place_id" while meaning nothing resolved."""
        with pytest.raises(ValidationError):
            resolved(place_id="")

    def test_it_becomes_a_poi_that_may_be_pinned(self):
        poi = resolved().to_poi(poi_id="poi-1")
        assert poi.place_id == "ChIJ_example"
        assert poi.is_verified is True

    def test_the_poi_records_places_as_its_source(self):
        """Not the search engine that suggested it: Places is what vouched for the location."""
        assert resolved().to_poi(poi_id="poi-1").source is PoiSource.PLACES

    def test_the_resulting_poi_can_be_put_on_the_route(self):
        """The `Poi` model refuses unverified pins; a resolved candidate must pass."""
        poi = resolved().to_poi(poi_id="poi-1", on_route=True)
        assert poi.on_route is True

    def test_it_keeps_the_original_claim_for_provenance(self):
        assert resolved().candidate.source == "brave"

    def test_the_poi_takes_the_resolved_category_not_the_queried_one(self):
        """A ski resort found by a dispersed-camping search is a ski resort.

        The category used to be inherited from the query, which mislabelled everything a
        query surfaced that was not what it asked for.
        """
        result = resolved(
            candidate=candidate(category=PoiCategory.WILD_CAMP),
            category=PoiCategory.HOTEL,
        )
        assert result.to_poi(poi_id="p1").category is PoiCategory.HOTEL

    def test_the_poi_carries_the_score_that_judged_it(self):
        """Persisted so selection can happen later, not only inside the run that scored it."""
        assert resolved().to_poi(poi_id="p1", score=0.85).score == 0.85

    def test_an_unjudged_place_has_no_score_rather_than_a_zero(self):
        assert resolved().to_poi(poi_id="p1").score is None

    def test_the_places_rating_still_does_not_survive_the_boundary(self):
        """Adding one storable number must not have opened the door to the unstorable ones."""
        poi = resolved(rating=4.8, user_rating_count=1200).to_poi(poi_id="p1", score=0.9)
        assert not hasattr(poi, "rating")

    def test_an_uncategorised_place_cannot_be_pinned(self):
        """The map needs an icon and the filters need a kind. Guessing either from the query
        is the bug this replaced, so refusing is the honest answer."""
        with pytest.raises(ValueError, match="category"):
            resolved(category=None).to_poi(poi_id="p1")


class TestEvidenceIsMeasuredNotAsserted:
    """What the judge is handed instead of being asked to estimate."""

    def test_it_carries_the_detour_off_the_route(self):
        assert Evidence(distance_off_route_m=1200.0).distance_off_route_m == 1200.0

    def test_a_negative_distance_is_refused(self):
        with pytest.raises(ValidationError):
            Evidence(distance_off_route_m=-1.0)

    def test_every_field_is_optional_because_not_everything_is_always_measurable(self):
        """Elevation gain has no source today; a scorer must handle absence, not a zero."""
        assert Evidence().twistiness_deg_per_km is None

    def test_absent_is_distinct_from_zero(self):
        """A provider that cannot report surface is not a road with no dirt on it."""
        assert Evidence().unpaved_fraction is None
        assert Evidence(unpaved_fraction=0.0).unpaved_fraction == 0.0

    def test_a_fraction_outside_zero_to_one_is_refused(self):
        with pytest.raises(ValidationError):
            Evidence(unpaved_fraction=1.5)


class TestScoring:
    def test_a_score_carries_its_reason(self):
        """An unexplained number cannot be argued with, and a rider will want to."""
        scored = ScoredCandidate(
            resolved=resolved(), evidence=Evidence(), score=0.8, reason="famous pass road"
        )
        assert scored.reason == "famous pass road"

    @pytest.mark.parametrize("score", [-0.1, 1.1])
    def test_a_score_outside_zero_to_one_is_refused(self, score):
        """The model will return 11 out of 10 if allowed to."""
        with pytest.raises(ValidationError):
            ScoredCandidate(resolved=resolved(), evidence=Evidence(), score=score, reason="x")

    def test_an_empty_reason_is_refused(self):
        """A score with no reason is the thing that makes the whole stage unreviewable."""
        with pytest.raises(ValidationError):
            ScoredCandidate(resolved=resolved(), evidence=Evidence(), score=0.5, reason="")

    def test_it_keeps_the_evidence_the_score_was_based_on(self):
        """So a human can check whether the judgement follows from the numbers."""
        evidence = Evidence(distance_off_route_m=800.0, unpaved_fraction=0.4)
        scored = ScoredCandidate(
            resolved=resolved(), evidence=evidence, score=0.9, reason="worth it"
        )
        assert scored.evidence.distance_off_route_m == 800.0
