"""API-layer exceptions.

Distinct from domain errors (`routing.errors`, `trips.errors`): these describe the state of
the HTTP surface itself, not of a trip or a routing engine.
"""


class NotImplementedYet(Exception):
    """A reserved endpoint whose schema is frozen but whose behaviour is not built.

    Raised rather than `HTTPException` so the response goes through the same envelope as
    every other error and carries a `code`. A client should be able to switch on
    `code == "not_implemented"` without special-casing the one family of endpoints that
    answers differently.
    """

    def __init__(self, what: str) -> None:
        super().__init__(f"{what} is not implemented yet")


class PlaceNotDisplayable(Exception):
    """Places answered, but not with enough to show the place.

    Distinct from `DiscoveryRefused`, which means the *provider* rejected the request — a bad
    key, a malformed query, a block — and is a 502 because it is ours to fix. This is a 422:
    the request was fine and the answer was thin, and the client can often do something about
    it, because the category it already holds is exactly what is missing.

    Borrowing `DiscoveryRefused` for this is what produced the bug. One exception meaning both
    "our key is wrong" and "send me a category" cannot map to one status honestly, so it
    mapped to neither and escaped as a 500.
    """
