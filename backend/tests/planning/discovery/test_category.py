"""What kind of place is this, actually?

The category used to come from the query that found it, which is how `Crystal Mountain
Resort` — a ski resort — ended up tagged `wild_camp`: it turned up in a dispersed-camping
search, so it inherited dispersed camping. That is systematic rather than a one-off, and it
mislabels everything a query surfaces that is not what the query asked for.

Order of preference, which is the usual rule here: Places' own `types` are deterministic and
free with a lookup already being made, so they win. The model is asked only where Places is
silent or cannot draw a distinction a rider cares about — dispersed camping has no Google
type at all, and `wild_camp` versus `campground` is a real difference to someone deciding
whether they need a booking.

The query's category is never used. Not as a fallback either: a plausible-looking wrong
category is worse than an absent one.
"""

import pytest

from motorooter.planning.discovery.category import (
    PLACES_TYPE_TO_CATEGORY,
    from_places_types,
    is_a_place,
)
from motorooter.trips.models import PoiCategory


class TestPlacesTypesDecideWhenTheyCan:
    @pytest.mark.parametrize(
        ("places_type", "expected"),
        [
            ("campground", PoiCategory.CAMPGROUND),
            ("rv_park", PoiCategory.CAMPGROUND),
            ("lodging", PoiCategory.HOTEL),
            ("hotel", PoiCategory.HOTEL),
            ("motel", PoiCategory.HOTEL),
            ("restaurant", PoiCategory.FOOD),
            ("cafe", PoiCategory.FOOD),
            ("gas_station", PoiCategory.FUEL),
            ("tourist_attraction", PoiCategory.VIEWPOINT),
            ("park", PoiCategory.VIEWPOINT),
            ("car_repair", PoiCategory.MECHANIC),
        ],
    )
    def test_a_known_type_maps(self, places_type, expected):
        assert from_places_types([places_type]) is expected

    def test_the_first_recognised_type_wins(self):
        """Places lists them roughly most-specific first."""
        assert from_places_types(["campground", "park"]) is PoiCategory.CAMPGROUND

    def test_unrecognised_types_are_skipped_not_fatal(self):
        assert from_places_types(["point_of_interest", "establishment", "cafe"]) is PoiCategory.FOOD

    def test_a_ski_resort_is_not_a_wild_camp(self):
        """The bug that prompted this: it inherited the query's category."""
        assert from_places_types(["ski_resort", "point_of_interest"]) is not PoiCategory.WILD_CAMP

    def test_no_usable_type_returns_nothing(self):
        """Rather than guessing. The model is asked next, and only then."""
        assert from_places_types(["point_of_interest", "establishment"]) is None

    def test_no_types_at_all_returns_nothing(self):
        assert from_places_types([]) is None

    def test_junk_entries_are_ignored(self):
        assert from_places_types([None, 42, "cafe"]) is PoiCategory.FOOD


class TestTheMappingItself:
    def test_every_mapped_value_is_a_real_category(self):
        for category in PLACES_TYPE_TO_CATEGORY.values():
            assert isinstance(category, PoiCategory)

    def test_wild_camp_is_deliberately_absent(self):
        """Google has no dispersed-camping type. Mapping something to it would be inventing
        a distinction Places does not draw, which is exactly what the model is for."""
        assert PoiCategory.WILD_CAMP not in PLACES_TYPE_TO_CATEGORY.values()

    def test_generic_container_types_are_absent(self):
        """`point_of_interest` and `establishment` are on almost everything; mapping them
        would make the first branch always fire and the model never run."""
        assert "point_of_interest" not in PLACES_TYPE_TO_CATEGORY
        assert "establishment" not in PLACES_TYPE_TO_CATEGORY


class TestTheModelFillsWhatPlacesCannot:
    """Batched, conditional, and only ever the residue."""

    @staticmethod
    def _resolved(name: str, category=None, types=(), snippet=None):
        from motorooter.planning.discovery.models import Candidate, ResolvedCandidate
        from motorooter.routing.models import Coordinate

        return ResolvedCandidate(
            candidate=Candidate(
                name=name,
                category=PoiCategory.WILD_CAMP,
                found_near=Coordinate(lat=47.0, lon=-121.0),
                source="brave",
                snippet=snippet,
            ),
            place_id="ChIJ_x",
            coordinate=Coordinate(lat=47.0, lon=-121.0),
            category=category,
            places_types=types,
        )

    @staticmethod
    def _says(payload):
        import json as _json

        from motorooter.llm.messages import AssistantMessage

        return AssistantMessage(content=_json.dumps(payload))

    async def test_it_assigns_a_category_places_could_not(self):
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(
            replies=(self._says({"categories": [{"index": 0, "category": "wild_camp"}]}),)
        )
        result = await CategoryClassifier(client).classify([self._resolved("Snag Lake")])
        assert result[0].category is PoiCategory.WILD_CAMP

    async def test_it_does_not_ask_about_places_already_typed(self):
        """Most places never reach the model; that is what keeps this off the fan-out."""
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(replies=(self._says({"categories": []}),))
        await CategoryClassifier(client).classify(
            [self._resolved("A diner", category=PoiCategory.FOOD)]
        )
        assert client.call_count == 0

    async def test_one_call_for_the_whole_batch(self):
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(replies=(self._says({"categories": []}),))
        await CategoryClassifier(client).classify([self._resolved("A"), self._resolved("B")])
        assert client.call_count == 1

    async def test_an_already_typed_place_is_not_overwritten(self):
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(
            replies=(self._says({"categories": [{"index": 0, "category": "hotel"}]}),)
        )
        result = await CategoryClassifier(client).classify(
            [self._resolved("A diner", category=PoiCategory.FOOD), self._resolved("B")]
        )
        assert result[0].category is PoiCategory.FOOD

    async def test_an_invented_category_is_discarded(self):
        """An absent category beats a plausible wrong one — that is the whole rule."""
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(
            replies=(self._says({"categories": [{"index": 0, "category": "ski_resort"}]}),)
        )
        result = await CategoryClassifier(client).classify([self._resolved("A")])
        assert result[0].category is None

    async def test_the_google_types_are_shown_as_evidence(self):
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(replies=(self._says({"categories": []}),))
        await CategoryClassifier(client).classify(
            [self._resolved("A", types=("point_of_interest", "campground_adjacent"))]
        )
        assert "campground_adjacent" in str(client.conversations[-1])

    async def test_the_wild_camp_distinction_is_explained(self):
        """Google does not draw it and a rider needs it before they arrive."""
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(replies=(self._says({"categories": []}),))
        await CategoryClassifier(client).classify([self._resolved("A")])
        assert "dispersed" in str(client.conversations[-1]).lower()

    async def test_a_malformed_reply_leaves_categories_unset(self):
        from motorooter.llm.messages import AssistantMessage
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(replies=(AssistantMessage(content="I am not sure."),))
        result = await CategoryClassifier(client).classify([self._resolved("A")])
        assert result[0].category is None


class TestARoadIsNeverAPlace:
    """Places types a highway, a byway and a forest road all as `route`.

    Asking the model to categorise one produces a plausible answer rather than a refusal:
    `Suntop Trail` and `Mather Memorial Highway` both came back as viewpoints on a live run,
    and would have been pinned as somewhere a rider could stop. Excluded on Places' own
    answer instead, which is deterministic and does not depend on a model declining.
    """

    def test_a_route_has_no_category(self):
        assert from_places_types(["route"]) is None

    def test_a_route_is_not_rescued_by_another_type(self):
        """`park` would otherwise map it, and a road through a park is still a road."""
        assert from_places_types(["route", "park"]) is None

    def test_a_real_place_is_unaffected(self):
        assert from_places_types(["tourist_attraction", "point_of_interest"]) is not None

    async def test_the_model_is_not_asked_about_roads(self):
        from motorooter.llm.messages import AssistantMessage
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier
        from motorooter.planning.discovery.models import Candidate, ResolvedCandidate
        from motorooter.routing.models import Coordinate

        road = ResolvedCandidate(
            candidate=Candidate(
                name="Mather Memorial Highway",
                category=PoiCategory.VIEWPOINT,
                found_near=Coordinate(lat=47.0, lon=-121.0),
                source="brave",
            ),
            place_id="ChIJ_road",
            coordinate=Coordinate(lat=47.0, lon=-121.0),
            places_types=("route",),
        )
        client = FakeLlmClient(replies=(AssistantMessage(content='{"categories": []}'),))
        result = await CategoryClassifier(client).classify([road])
        assert client.call_count == 0
        assert result[0].category is None


class TestARoadJunctionIsNotAPlace:
    """`Sunset Way & 6th Ave NE, Issaquah` came back on three of four live runs.

    It resolved, it was scored, and it was then dropped for having no category — a metered
    Places lookup and a scoring slot spent on somewhere nobody can stop. Worse, without this
    the classifier *asks the model* about it, and a model asked to categorise a junction
    answers rather than declining, exactly as it did for two highways.
    """

    def test_a_junction_is_refused(self):
        assert not is_a_place(["intersection"])

    async def test_the_model_is_not_asked_about_one(self):
        """The saving and the correctness fix are the same line: refused before the model
        sees it, rather than after it has invented an answer."""
        from motorooter.llm.providers.fake import FakeLlmClient
        from motorooter.planning.discovery.category import CategoryClassifier

        client = FakeLlmClient(
            replies=(
                TestTheModelFillsWhatPlacesCannot._says(
                    {"categories": [{"index": 0, "category": "viewpoint"}]}
                ),
            )
        )
        resolved = TestTheModelFillsWhatPlacesCannot._resolved(
            "Sunset Way & 6th Ave NE", types=("intersection",)
        )
        result = await CategoryClassifier(client).classify([resolved])
        assert client.call_count == 0
        assert result[0].category is None
