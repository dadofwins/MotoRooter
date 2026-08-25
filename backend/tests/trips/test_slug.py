"""Trip slug validation.

Slugs come from untrusted user input and become Cloud Storage object paths, so this is a
security boundary, not a formatting nicety. Every rejection case here is a real attack or
a real corruption path.
"""

import pytest

from motorooter.trips.slug import MAX_SLUG_LENGTH, InvalidSlug, slugify, validate_slug


class TestSlugify:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Oregon Backcountry", "oregon-backcountry"),
            ("WABDR 2026", "wabdr-2026"),
            ("  leading and trailing  ", "leading-and-trailing"),
            ("multiple   spaces", "multiple-spaces"),
            ("Trip: Part 2!", "trip-part-2"),
            ("under_scores", "under-scores"),
            ("already-a-slug", "already-a-slug"),
        ],
    )
    def test_converts_names_to_slugs(self, name, expected):
        assert slugify(name) == expected

    def test_collapses_repeated_separators(self):
        assert slugify("a---b___c") == "a-b-c"

    def test_strips_leading_and_trailing_separators(self):
        assert slugify("---trip---") == "trip"

    def test_transliterates_accented_characters(self):
        """Riders name trips after places. Dropping the letter entirely mangles the name."""
        assert slugify("Col du Galibier") == "col-du-galibier"
        assert slugify("Ruta 40 Español") == "ruta-40-espanol"

    def test_truncates_overlong_input(self):
        assert len(slugify("a" * 500)) == MAX_SLUG_LENGTH

    def test_truncation_does_not_leave_a_trailing_separator(self):
        name = ("word " * 100).strip()
        assert not slugify(name).endswith("-")

    def test_name_with_no_usable_characters_raises(self):
        """Better an explicit error than a silent empty object path."""
        with pytest.raises(InvalidSlug):
            slugify("!!!")

    def test_empty_name_raises(self):
        with pytest.raises(InvalidSlug):
            slugify("   ")


class TestValidateSlug:
    def test_accepts_a_well_formed_slug(self):
        assert validate_slug("oregon-backcountry") == "oregon-backcountry"

    @pytest.mark.parametrize(
        "attack",
        [
            "../etc/passwd",
            "..",
            ".",
            "a/../../b",
            "trips/other-trip",
            "nested/path",
            "back\\slash",
            "leading/",
            "/absolute",
        ],
    )
    def test_rejects_path_traversal_and_separators(self, attack):
        """Slugs become object paths; a separator escapes the trip's prefix."""
        with pytest.raises(InvalidSlug):
            validate_slug(attack)

    @pytest.mark.parametrize(
        "attack",
        ["trip\x00null", "trip\nnewline", "trip\ttab", "trip\r"],
    )
    def test_rejects_control_characters(self, attack):
        with pytest.raises(InvalidSlug):
            validate_slug(attack)

    @pytest.mark.parametrize("bad", ["Trip", "UPPER", "MiXeD"])
    def test_rejects_uppercase(self, bad):
        """Cloud Storage keys are case-sensitive; two casings would be two silent trips."""
        with pytest.raises(InvalidSlug):
            validate_slug(bad)

    @pytest.mark.parametrize("bad", ["trip name", "trip.json", "trip%20", "trip?a=1", "trip#frag"])
    def test_rejects_characters_outside_the_allowed_set(self, bad):
        with pytest.raises(InvalidSlug):
            validate_slug(bad)

    def test_rejects_empty(self):
        with pytest.raises(InvalidSlug):
            validate_slug("")

    def test_rejects_overlong(self):
        with pytest.raises(InvalidSlug):
            validate_slug("a" * (MAX_SLUG_LENGTH + 1))

    def test_accepts_maximum_length(self):
        assert validate_slug("a" * MAX_SLUG_LENGTH)

    @pytest.mark.parametrize("bad", ["-leading", "trailing-", "-both-"])
    def test_rejects_leading_or_trailing_separators(self, bad):
        with pytest.raises(InvalidSlug):
            validate_slug(bad)

    @pytest.mark.parametrize("reserved", ["new", "index", "api", "static", "assets", "health"])
    def test_rejects_reserved_names(self, reserved):
        """These collide with routes or with objects the app writes itself."""
        with pytest.raises(InvalidSlug):
            validate_slug(reserved)

    def test_error_explains_the_rule(self):
        with pytest.raises(InvalidSlug, match="lowercase"):
            validate_slug("Trip")


class TestRoundTrip:
    @pytest.mark.parametrize(
        "name",
        ["Oregon Backcountry", "WABDR 2026", "Col du Galibier", "a" * 500, "Trip: Part 2!"],
    )
    def test_slugify_output_always_passes_validation(self, name):
        """The two functions must never disagree, or valid names become unsaveable trips."""
        assert validate_slug(slugify(name))

    def test_slugify_avoids_producing_a_reserved_name(self):
        assert validate_slug(slugify("API"))
