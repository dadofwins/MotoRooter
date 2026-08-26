"""Roads are leads, not results.

`Route 410` scoring 0.95 was not the pipeline being wrong about what is interesting. It was
right, and then stopping one step short: a rider cannot pull over at a scenic byway, they
ride it. But "this road is worth riding" is exactly the signal that should produce "and here
is the viewpoint on it worth stopping for".

That was visible in the endpoint's first live run, where the only two candidates surviving
resolution were WA-410 and WA-123 — the pipeline confidently identifying the best things on
the route and then discarding both, because neither can be pinned.

The cost is a genuine fan-out multiplier, so expansion is capped at one hop. A road found by
expanding a road does not expand again, or this walks the highway network.
"""

from motorooter.planning.discovery.expansion import expansion_queries, roads_and_places
from motorooter.planning.discovery.models import Candidate, CandidateKind
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory

ANCHOR = Coordinate(lat=46.87, lon=-121.52)


def candidate(name: str, kind: CandidateKind = CandidateKind.PLACE, **extra) -> Candidate:
    return Candidate(
        name=name,
        category=PoiCategory.VIEWPOINT,
        found_near=ANCHOR,
        source="brave",
        kind=kind,
        **extra,
    )


class TestSplittingLeadsFromResults:
    def test_places_and_roads_are_separated(self):
        places, roads = roads_and_places(
            [candidate("Suntop Lookout"), candidate("WA-410", CandidateKind.ROAD)]
        )
        assert [p.name for p in places] == ["Suntop Lookout"]
        assert [r.name for r in roads] == ["WA-410"]

    def test_an_unclassified_candidate_is_treated_as_a_place(self):
        """A source that does not classify should still produce results."""
        places, _ = roads_and_places([candidate("Somewhere")])
        assert len(places) == 1

    def test_a_road_that_was_itself_found_by_expansion_is_not_expanded(self):
        """One hop. Otherwise this walks the highway network, one search per road."""
        _, roads = roads_and_places([candidate("WA-123", CandidateKind.ROAD, found_via="WA-410")])
        assert roads == ()

    def test_a_place_found_by_expansion_is_still_a_result(self):
        """Only roads stop expanding; the places they surface are the point."""
        places, _ = roads_and_places([candidate("Suntop Lookout", found_via="WA-410")])
        assert len(places) == 1

    def test_duplicate_roads_are_expanded_once(self):
        """Two anchors naming the same byway should not pay for it twice."""
        _, roads = roads_and_places(
            [candidate("WA-410", CandidateKind.ROAD), candidate("wa-410", CandidateKind.ROAD)]
        )
        assert len(roads) == 1


class TestTheQueriesARoadProduces:
    def test_it_asks_what_is_worth_stopping_for_on_that_road(self):
        queries = expansion_queries(candidate("Chinook Scenic Byway", CandidateKind.ROAD))
        assert all("Chinook Scenic Byway" in query.text for query in queries)

    def test_the_queries_are_about_stopping_not_riding(self):
        """The road is already known to be good; what is wanted is places along it."""
        texts = " ".join(
            query.text for query in expansion_queries(candidate("WA-410", CandidateKind.ROAD))
        )
        assert "viewpoint" in texts or "pull" in texts

    def test_the_fan_out_per_road_is_bounded(self):
        """Roads times queries times anchors is how a corridor becomes a bill."""
        assert len(expansion_queries(candidate("WA-410", CandidateKind.ROAD))) <= 3

    def test_a_place_produces_no_expansion_queries(self):
        assert expansion_queries(candidate("Suntop Lookout")) == ()


class TestProvenanceSurvives:
    def test_the_road_is_recorded_on_what_it_surfaced(self):
        """A viewpoint on a road people ride for pleasure is worth more than the same
        viewpoint on a road nobody mentions, and only this carries that."""
        found = candidate("Suntop Lookout", found_via="Chinook Scenic Byway")
        assert found.found_via == "Chinook Scenic Byway"

    def test_a_candidate_found_directly_has_no_road(self):
        assert candidate("Suntop Lookout").found_via is None
