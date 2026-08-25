"""Turning a place and a category into a web search.

Search is the only stage that can find out a road is *good*. Places knows a restaurant exists
and its rating; it does not know locals ride a pass for fun. That lives in ride reports and
forum threads, and the query is what reaches them — so the query is the product, not
plumbing. "campsite 47.0,-121.0" finds nothing a rider wants; "wild camping near Chinook Pass
motorcycle" finds the thread where someone describes the pull-off.
"""

import pytest

from motorooter.planning.discovery.queries import CATEGORY_TEMPLATES, queries_for
from motorooter.trips.models import PoiCategory


class TestTheQueryReadsLikeSomethingAHumanWouldSearch:
    def test_it_names_the_place(self):
        found = queries_for("Chinook Pass", [PoiCategory.WILD_CAMP])
        assert all("Chinook Pass" in query.text for query in found)

    def test_it_says_motorcycle_somewhere(self):
        """Without it the results are car-camping and family restaurants."""
        found = queries_for("Naches", [PoiCategory.CAMPGROUND, PoiCategory.FOOD])
        assert all(
            "motorcycle" in query.text.lower() or "moto" in query.text.lower() for query in found
        )

    def test_it_never_puts_coordinates_in_the_query(self):
        """A web index has no idea what 47.0,-121.0 is. This is the whole reason the anchor
        has to be turned into a name before searching."""
        found = queries_for("Cle Elum", list(PoiCategory))
        assert not any(any(c.isdigit() for c in query.text) for query in found)

    def test_every_category_has_a_template(self):
        """A missing one would silently search nothing for that category."""
        for category in PoiCategory:
            assert category in CATEGORY_TEMPLATES

    def test_each_category_gets_its_own_wording(self):
        """A generic query returns generic results; a wild camp is not a hotel."""
        camp = queries_for("Naches", [PoiCategory.WILD_CAMP])[0].text
        hotel = queries_for("Naches", [PoiCategory.HOTEL])[0].text
        assert camp != hotel

    def test_the_query_carries_the_category_it_came_from(self):
        """So a result can be labelled without guessing from its text."""
        found = queries_for("Naches", [PoiCategory.FUEL])
        assert found[0].category is PoiCategory.FUEL

    def test_the_query_carries_the_place_it_came_from(self):
        assert queries_for("Naches", [PoiCategory.FUEL])[0].place == "Naches"


class TestTheFanOutIsBounded:
    def test_one_query_per_category(self):
        """Anchors times categories is the metered request count; it must be predictable."""
        categories = [PoiCategory.WILD_CAMP, PoiCategory.FOOD, PoiCategory.FUEL]
        assert len(queries_for("Naches", categories)) == 3

    def test_no_categories_is_no_queries(self):
        assert queries_for("Naches", []) == ()

    def test_duplicate_categories_do_not_double_the_cost(self):
        """A caller passing a category twice should not pay twice."""
        assert len(queries_for("Naches", [PoiCategory.FOOD, PoiCategory.FOOD])) == 1

    def test_an_empty_place_is_refused(self):
        """It would produce a query with no location at all, and spend a request on it."""
        with pytest.raises(ValueError):
            queries_for("", [PoiCategory.FOOD])

    def test_a_whitespace_place_is_refused(self):
        with pytest.raises(ValueError):
            queries_for("   ", [PoiCategory.FOOD])
