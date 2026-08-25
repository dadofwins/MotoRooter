"""Trip slug generation and validation.

A slug is a user-chosen trip name turned into a URL path segment and a Cloud Storage
object prefix (`trips/<slug>/trip.json`). Because it reaches both, it is untrusted input
crossing into a path context, and is validated as a security boundary rather than trusted
from the caller.

`validate_slug` is deliberately strict and allowlist-based: anything not explicitly
permitted is rejected. `slugify` is the lenient front door that turns a human name into
something `validate_slug` will accept.
"""

import re
import unicodedata

MAX_SLUG_LENGTH = 64
"""Long enough for a descriptive name, short enough to stay a comfortable path segment."""

_ALLOWED = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""Lowercase alphanumerics in hyphen-separated groups. No leading, trailing, or doubled hyphens."""

RESERVED_SLUGS = frozenset(
    {
        # Would shadow an API or static route.
        "api",
        "assets",
        "static",
        "health",
        "docs",
        "openapi",
        # UI routes that are not trips.
        "new",
        "index",
        "edit",
        "login",
        "admin",
        # Objects the app writes alongside trip prefixes.
        "trips",
        "_manifest",
    }
)


class InvalidSlug(ValueError):
    """A trip name or slug that cannot be used safely."""


def validate_slug(slug: str) -> str:
    """Return `slug` unchanged if it is safe to use as a path segment.

    Raises:
        InvalidSlug: with a message stating the rule that was broken, so the API can
            surface something actionable rather than a generic 400.
    """
    if not slug:
        msg = "trip slug must not be empty"
        raise InvalidSlug(msg)

    if len(slug) > MAX_SLUG_LENGTH:
        msg = f"trip slug must be at most {MAX_SLUG_LENGTH} characters, got {len(slug)}"
        raise InvalidSlug(msg)

    # Checked before the pattern so the error names the actual problem. The pattern would
    # reject these anyway; this only improves the message.
    if any(char in slug for char in ("/", "\\", "\x00")):
        msg = "trip slug must not contain path separators or null bytes"
        raise InvalidSlug(msg)

    if not _ALLOWED.match(slug):
        msg = (
            "trip slug must be lowercase letters, digits, and single hyphens, "
            "with no leading or trailing hyphen"
        )
        raise InvalidSlug(msg)

    if slug in RESERVED_SLUGS:
        msg = f"trip slug {slug!r} is reserved"
        raise InvalidSlug(msg)

    return slug


def slugify(name: str) -> str:
    """Turn a human trip name into a valid slug.

    Accents are transliterated rather than dropped, since riders name trips after places
    ("Col du Galibier", "Ruta 40 Español") and dropping the letter mangles the name.

    Raises:
        InvalidSlug: the name contains no usable characters.
    """
    # NFKD splits accented characters into base + combining mark; dropping the marks
    # leaves the ASCII base letter.
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")

    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    collapsed = hyphenated.strip("-")

    if len(collapsed) > MAX_SLUG_LENGTH:
        # Truncation can land mid-group and leave a trailing hyphen, which the validator
        # rejects; strip it after cutting rather than before.
        collapsed = collapsed[:MAX_SLUG_LENGTH].rstrip("-")

    if not collapsed:
        msg = f"trip name {name!r} contains no characters usable in a slug"
        raise InvalidSlug(msg)

    if collapsed in RESERVED_SLUGS:
        # Suffix rather than reject: the user's name is fine, it just collides with a
        # route. Length is already within budget since the reserved words are short.
        collapsed = f"{collapsed}-trip"

    return collapsed
