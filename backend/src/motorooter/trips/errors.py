"""Trip storage errors, mapped to HTTP status codes at the API boundary."""


class TripError(Exception):
    """Base for trip storage failures."""


class TripNotFound(TripError):
    """No trip exists at that slug. Maps to 404."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"no trip named {slug!r}")


class TripAlreadyExists(TripError):
    """A trip already occupies that slug. Maps to 409.

    Trip names are the primary key and everything is public, so creation refuses to
    clobber. Callers that intend to replace use `put`.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"a trip named {slug!r} already exists")


class TripStorageUnavailable(TripError):
    """The backing store could not be reached. Maps to 503."""
