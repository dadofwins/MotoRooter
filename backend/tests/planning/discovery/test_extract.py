"""Reading place names out of search results.

The stage the spike proved was missing. Search returns pages *about* places — directory
listings, magazine articles, reddit threads — and the place names are in the prose rather
than the title. Extracting one is a language task, so a model does it; believing what it
returns is not, so the output is checked against its source.

Everything here runs against a scripted client. What the model "says" is whatever the test
tells it to say, which is the only way to exercise a model that invents places, answers with
prose instead of JSON, or names something from the wrong article.
"""

import json

import pytest

from motorooter.llm.messages import AssistantMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.planning.discovery.extract import PlaceExtractor
from motorooter.planning.discovery.models import Candidate
from motorooter.routing.models import Coordinate
from motorooter.trips.models import PoiCategory

ANCHOR = Coordinate(lat=46.87, lon=-121.52)

RESULTS = (
    Candidate(
        name="Dispersed Camping near Chinook, WA",
        category=PoiCategory.WILD_CAMP,
        found_near=ANCHOR,
        source="brave",
        snippet=(
            "the most popular dispersed campground near Chinook, WA is Road to Snag Lake "
            "- Dispersed with a 4.4-star rating from 15 reviews."
        ),
        url="https://thedyrt.test/x",
    ),
    Candidate(
        name="Okanogan-Wenatchee: Halfway Flat Dispersed Campground",
        category=PoiCategory.WILD_CAMP,
        found_near=ANCHOR,
        source="brave",
        snippet="These dispersed camping sites are located outside of the campground area.",
        url="https://fs.usda.test/y",
    ),
)


def says(payload: object) -> AssistantMessage:
    return AssistantMessage(content=json.dumps(payload))


def extraction(index: int, place: str, *, relevant: bool = True) -> dict[str, object]:
    return {"result_index": index, "place_name": place, "relevant": relevant}


def extractor(*replies: AssistantMessage) -> tuple[PlaceExtractor, FakeLlmClient]:
    client = FakeLlmClient(replies=replies)
    return PlaceExtractor(client), client


class TestItNamesRealPlacesFromProse:
    async def test_it_replaces_a_page_title_with_the_place_it_describes(self):
        extract, _ = extractor(says({"places": [extraction(0, "Road to Snag Lake")]}))
        found = await extract.extract(RESULTS)
        assert [candidate.name for candidate in found] == ["Road to Snag Lake"]

    async def test_it_keeps_the_snippet_that_justified_it(self):
        """The judge still needs the prose; extraction narrows the name, not the evidence."""
        extract, _ = extractor(says({"places": [extraction(0, "Road to Snag Lake")]}))
        found = await extract.extract(RESULTS)
        assert "4.4-star" in (found[0].snippet or "")

    async def test_it_keeps_the_url_so_a_human_can_check(self):
        extract, _ = extractor(says({"places": [extraction(0, "Road to Snag Lake")]}))
        assert (await extract.extract(RESULTS))[0].url == "https://thedyrt.test/x"

    async def test_it_keeps_the_category_and_the_anchor(self):
        extract, _ = extractor(says({"places": [extraction(0, "Road to Snag Lake")]}))
        found = await extract.extract(RESULTS)
        assert found[0].category is PoiCategory.WILD_CAMP
        assert found[0].found_near == ANCHOR

    async def test_one_result_can_yield_several_places(self):
        """A directory page legitimately lists more than one."""
        extract, _ = extractor(
            says(
                {
                    "places": [
                        extraction(0, "Road to Snag Lake"),
                        extraction(0, "dispersed campground"),
                    ]
                }
            )
        )
        assert len(await extract.extract(RESULTS)) == 2

    async def test_it_records_that_extraction_touched_the_candidate(self):
        """Provenance: this name came from a model reading a page, not from the page title."""
        extract, _ = extractor(says({"places": [extraction(0, "Road to Snag Lake")]}))
        assert "brave" in (await extract.extract(RESULTS))[0].source


class TestTheGuardIsApplied:
    """A prompt instruction is not a guard. This is the check on the output."""

    async def test_an_invented_place_is_dropped(self):
        extract, _ = extractor(says({"places": [extraction(0, "Bear Hollow Campground")]}))
        assert await extract.extract(RESULTS) == ()

    async def test_a_place_from_the_wrong_result_is_dropped(self):
        """Naming result 1's place against result 0 is how a model conflates two pages."""
        extract, _ = extractor(says({"places": [extraction(0, "Halfway Flat")]}))
        assert await extract.extract(RESULTS) == ()

    async def test_a_grounded_place_survives_alongside_a_dropped_one(self):
        extract, _ = extractor(
            says(
                {
                    "places": [
                        extraction(0, "Bear Hollow Campground"),
                        extraction(0, "Road to Snag Lake"),
                    ]
                }
            )
        )
        assert [c.name for c in await extract.extract(RESULTS)] == ["Road to Snag Lake"]

    async def test_an_out_of_range_result_index_is_dropped(self):
        extract, _ = extractor(says({"places": [extraction(99, "Road to Snag Lake")]}))
        assert await extract.extract(RESULTS) == ()

    async def test_a_negative_index_is_dropped_rather_than_wrapping(self):
        """Python would index from the end and ground against the wrong source."""
        extract, _ = extractor(says({"places": [extraction(-1, "Road to Snag Lake")]}))
        assert await extract.extract(RESULTS) == ()


class TestIrrelevantResultsAreDropped:
    async def test_a_result_marked_irrelevant_is_dropped(self):
        """The Northern California article answering a Washington query."""
        extract, _ = extractor(
            says({"places": [extraction(0, "Road to Snag Lake", relevant=False)]})
        )
        assert await extract.extract(RESULTS) == ()

    async def test_relevance_defaults_to_true_when_omitted(self):
        """A missing field should not silently discard everything."""
        extract, _ = extractor(
            says({"places": [{"result_index": 0, "place_name": "Road to Snag Lake"}]})
        )
        assert len(await extract.extract(RESULTS)) == 1


class TestItNamesDestinationsNotRegions:
    """Found by running it: grounded is not the same as useful.

    The first live run turned one snippet into "Road to Snag Lake" — correct — and also
    "Chinook, WA", "Okanogan-Wenatchee National Forest", "Mt. Rainer National Park" and
    "Chinook Pass". Every one passes grounding, because every one is in the text. None is
    somewhere a rider stops; they are the geography the real place sits inside, and pinning
    a national forest on a map is worse than pinning nothing.
    """

    async def test_the_place_being_searched_is_not_itself_a_candidate(self):
        """The corridor anchor is context, not a discovery. Dropped deterministically —
        we know what we searched for, so this does not depend on the model behaving."""
        extract, _ = extractor(says({"places": [extraction(0, "Chinook, WA")]}))
        assert await extract.extract(RESULTS, searched_for="Chinook, WA") == ()

    async def test_the_search_place_is_matched_loosely(self):
        extract, _ = extractor(says({"places": [extraction(0, "chinook  wa")]}))
        assert await extract.extract(RESULTS, searched_for="Chinook, WA") == ()

    async def test_a_real_place_near_the_search_place_survives(self):
        extract, _ = extractor(says({"places": [extraction(0, "Road to Snag Lake")]}))
        found = await extract.extract(RESULTS, searched_for="Chinook, WA")
        assert [c.name for c in found] == ["Road to Snag Lake"]

    async def test_the_prompt_says_not_to_name_containing_regions(self):
        """The deterministic drop only covers the one place we know we searched."""
        _, client = extractor(says({"places": []}))
        await PlaceExtractor(client).extract(RESULTS)
        sent = str(client.conversations[-1]).lower()
        assert "national forest" in sent


class TestDuplicatesAreCollapsed:
    async def test_the_same_place_named_twice_appears_once(self):
        """Two results about one campground is the normal case, not an edge case."""
        extract, _ = extractor(
            says(
                {
                    "places": [
                        extraction(0, "Road to Snag Lake"),
                        extraction(1, "road to snag lake"),
                    ]
                }
            )
        )
        assert len(await extract.extract(RESULTS)) == 1

    async def test_punctuation_differences_still_count_as_duplicates(self):
        extract, _ = extractor(
            says(
                {
                    "places": [
                        extraction(0, "Road to Snag Lake"),
                        extraction(0, "Road to Snag Lake."),
                    ]
                }
            )
        )
        assert len(await extract.extract(RESULTS)) == 1

    async def test_the_first_spelling_is_the_one_kept(self):
        extract, _ = extractor(
            says(
                {
                    "places": [
                        extraction(0, "Road to Snag Lake"),
                        extraction(0, "road to snag lake"),
                    ]
                }
            )
        )
        assert (await extract.extract(RESULTS))[0].name == "Road to Snag Lake"


class TestThePerResultCap:
    async def test_one_result_cannot_produce_unlimited_places(self):
        """Each survivor costs a Places lookup, so the model does not set that budget."""
        extract, _ = extractor(
            says(
                {
                    "places": [
                        extraction(0, "Road to Snag Lake"),
                        extraction(0, "dispersed campground"),
                        extraction(0, "Chinook"),
                    ]
                }
            )
        )
        assert len(await extract.extract(RESULTS, max_per_result=2)) == 2


class TestAMisbehavingModel:
    """None of these may raise. Extraction failing should cost candidates, not the run."""

    @pytest.mark.parametrize(
        "reply",
        [
            AssistantMessage(content="I could not find any places, sorry."),
            AssistantMessage(content="{not json"),
            AssistantMessage(content=""),
            AssistantMessage(content=None),
            AssistantMessage(content='{"places": "not-a-list"}'),
            AssistantMessage(content='{"places": [null]}'),
            AssistantMessage(content='{"places": ["a string"]}'),
            AssistantMessage(content='{"places": [{}]}'),
            AssistantMessage(content='{"wrong_key": []}'),
            AssistantMessage(content="[]"),
        ],
    )
    async def test_a_malformed_reply_yields_nothing_rather_than_raising(self, reply):
        extract, _ = extractor(reply)
        assert await extract.extract(RESULTS) == ()

    async def test_json_wrapped_in_a_code_fence_is_still_read(self):
        """Models do this constantly, and losing every candidate to it would be silly."""
        payload = json.dumps({"places": [extraction(0, "Road to Snag Lake")]})
        extract, _ = extractor(AssistantMessage(content=f"```json\n{payload}\n```"))
        assert len(await extract.extract(RESULTS)) == 1

    async def test_prose_around_the_json_is_tolerated(self):
        payload = json.dumps({"places": [extraction(0, "Road to Snag Lake")]})
        extract, _ = extractor(
            AssistantMessage(content=f"Here is what I found:\n{payload}\nHope that helps.")
        )
        assert len(await extract.extract(RESULTS)) == 1


class TestTheCallIsBounded:
    async def test_one_call_for_the_whole_batch(self):
        """Per-candidate would put extraction on the fan-out multiplier."""
        _, client = extractor(says({"places": []}))
        await PlaceExtractor(client).extract(RESULTS)
        assert client.call_count == 1

    async def test_an_empty_batch_calls_nothing(self):
        _, client = extractor(says({"places": []}))
        assert await PlaceExtractor(client).extract(()) == ()
        assert client.call_count == 0

    async def test_the_prompt_carries_every_result(self):
        _, client = extractor(says({"places": []}))
        await PlaceExtractor(client).extract(RESULTS)
        sent = str(client.conversations[-1])
        assert "Snag Lake" in sent
        assert "Halfway Flat" in sent


class TestRegionDisambiguation:
    async def test_the_region_is_offered_to_the_model(self):
        """ "Cayuse" matched Oregon on a Washington corridor. The anchor knows better."""
        _, client = extractor(says({"places": []}))
        await PlaceExtractor(client).extract(RESULTS, region="Washington")
        assert "Washington" in str(client.conversations[-1])

    async def test_a_region_qualified_name_still_passes_grounding(self):
        extract, _ = extractor(says({"places": [extraction(0, "Road to Snag Lake, Washington")]}))
        found = await extract.extract(RESULTS, region="Washington")
        assert found[0].name == "Road to Snag Lake, Washington"
