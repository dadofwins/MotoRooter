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
