"""Checking that an extracted place name came from the text it claims to have come from.

Extraction asks a model to read a snippet and name the places it is about. That is squarely a
language task and the right thing to ask a model for. It is also one short step from "name
the places in this text" to "name some plausible places", and the difference matters more
here than in most places a model is wrong: a nonexistent campsite is not a bad answer, it is
a rider on a forest road at dusk looking for something that was never there.

A prompt instruction is not a guard — the model may or may not follow it, and nothing checks.
This is applied to the output.

**Deliberately strict.** Substring matching rejects a genuine place the model reworded
slightly, and that costs one candidate out of several. Accepting an invented one costs
someone their evening. The asymmetry is the whole reason for the design.
"""

import re
import unicodedata

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercased, unaccented, punctuation-free, single-spaced.

    So "Halfway   Flat\\nCampground" and "halfway flat campground." are the same phrase, and
    a model reformatting a name does not fail a check about whether it invented it.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    without_punctuation = _PUNCTUATION.sub(" ", ascii_only.lower())
    return _WHITESPACE.sub(" ", without_punctuation).strip()


def appears_in(name: str, source_text: str, *, region: str | None = None) -> bool:
    """Whether `name` occurs in `source_text` as a contiguous phrase.

    Args:
        name: the extracted place name.
        source_text: everything the model was shown for this result — title and snippet
            together, since the place is often only in the title.
        region: a qualifier this system appended for disambiguation ("Washington"). Stripped
            before checking, so our own addition cannot fail its own test — but only from the
            end, so it cannot be used to launder an invented name.

    Contiguous rather than word-by-word on purpose. "Chinook Pass" and "camping" both appear
    in a snippet about Chinook Pass; "Chinook Pass Campground" does not, and it is precisely
    the plausible-sounding invention worth catching.
    """
    candidate = normalize(name)
    if not candidate:
        return False

    if region:
        suffix = f" {normalize(region)}"
        if candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)].strip()
        if not candidate:
            return False

    return candidate in normalize(source_text)
