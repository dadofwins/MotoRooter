"""The guard that stops the model inventing a campsite.

Extraction asks a model to read a web snippet and name the places it is about. That is a
language task and the right thing to ask a model for — but it is one short step from "name
the places in this text" to "name some plausible places", and the second one puts a
nonexistent campsite in front of a rider looking for somewhere to sleep.

So an extracted name is only accepted if it actually appears in the text it came from. Not a
prompt instruction, which a model may or may not follow — a check, applied to the output.

The check is deliberately strict. Rejecting a real place costs one missed candidate out of
several. Accepting an invented one sends someone down a forest road at dusk to a place that
does not exist, and Places will happily fail to resolve it *after* it has been shown.
"""

import pytest

from motorooter.planning.discovery.grounding import appears_in

SNIPPET = (
    "These dispersed camping sites are located outside of the Halfway Flat Campground "
    "area. Conveniently located 30 minutes below the summit of Chinook Pass."
)


class TestWhatIsGrounded:
    def test_an_exact_phrase_is_accepted(self):
        assert appears_in("Halfway Flat Campground", SNIPPET) is True

    def test_case_does_not_matter(self):
        assert appears_in("halfway flat campground", SNIPPET) is True

    def test_extra_whitespace_does_not_matter(self):
        assert appears_in("Halfway   Flat\nCampground", SNIPPET) is True

    def test_surrounding_punctuation_does_not_matter(self):
        assert appears_in("Chinook Pass.", SNIPPET) is True

    def test_a_name_spanning_title_and_snippet_is_checked_against_both(self):
        """Sources are searched as one text; the place is often only in the title."""
        combined = "Okanogan-Wenatchee: Halfway Flat Dispersed Campground\n" + SNIPPET
        assert appears_in("Halfway Flat Dispersed Campground", combined) is True


class TestWhatIsNot:
    def test_an_invented_place_is_rejected(self):
        assert appears_in("Bear Hollow Campground", SNIPPET) is False

    def test_a_name_assembled_from_scattered_words_is_rejected(self):
        """ "Chinook Pass" and "camping" both appear. "Chinook Pass Campground" does not,
        and it is exactly the plausible-sounding invention this guard exists to catch."""
        assert appears_in("Chinook Pass Campground", SNIPPET) is False

    def test_a_reworded_name_is_rejected_even_though_it_is_probably_real(self):
        """The deliberate cost. One missed candidate beats one invented one."""
        assert appears_in("Halfway Flat Dispersed Site", SNIPPET) is False

    def test_an_empty_name_is_rejected(self):
        assert appears_in("", SNIPPET) is False

    def test_a_whitespace_name_is_rejected(self):
        assert appears_in("   ", SNIPPET) is False

    def test_empty_source_text_grounds_nothing(self):
        assert appears_in("Halfway Flat Campground", "") is False


class TestRegionQualifiersAreAllowed:
    """Disambiguation is added by us, so it must not fail its own grounding check."""

    def test_a_trailing_region_is_ignored_when_checking(self):
        """ "Cayuse Pass, Washington" is grounded if "Cayuse Pass" is in the text."""
        assert appears_in("Chinook Pass, Washington", SNIPPET, region="Washington") is True

    def test_the_region_does_not_launder_an_invented_place(self):
        assert appears_in("Bear Hollow, Washington", SNIPPET, region="Washington") is False

    def test_a_region_that_was_not_appended_changes_nothing(self):
        assert appears_in("Halfway Flat Campground", SNIPPET, region="Oregon") is True


@pytest.mark.parametrize(
    "name",
    [
        "Halfway Flat Campground",
        "Chinook Pass",
        "dispersed camping sites",
    ],
)
def test_real_extractions_from_a_real_snippet_pass(name):
    assert appears_in(name, SNIPPET) is True
