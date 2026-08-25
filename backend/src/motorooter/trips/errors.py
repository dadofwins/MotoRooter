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


class TripDocumentInvalid(TripError):
    """A stored trip document could not be read back as a `Trip`. Maps to 500.

    Corruption, or a document written by a newer schema version than this build
    understands. Deliberately not a 404: the trip exists, and telling the user it does not
    invites them to recreate it over the top of data that might still be recoverable.
    """

    def __init__(self, slug: str, reason: str) -> None:
        self.slug = slug
        super().__init__(f"stored trip {slug!r} could not be read: {reason}")


class TripStorageConfigError(TripError):
    """Storage wiring is inconsistent — raised at startup, never mid-request.

    Same reasoning as `RoutingConfigError`: a bucket name that cannot be addressed should
    fail the deploy, not the first save of the day.
    """
