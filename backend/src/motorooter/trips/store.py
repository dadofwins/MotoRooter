"""Trip persistence.

`TripStore` is the seam between the API and wherever trips actually live. The in-memory
implementation covers development and tests; the Cloud Storage implementation writes
`trips/<slug>/trip.json` and must pass the same contract suite
(`tests/trips/store_contract.py`) unchanged.

Note for any persistent implementation: Cloud Run's container filesystem is ephemeral and
per-instance, so trips must go to a bucket, never to local disk.
"""

from typing import Protocol, runtime_checkable

from motorooter.trips.errors import TripAlreadyExists, TripNotFound
from motorooter.trips.models import Trip, TripSummary


@runtime_checkable
class TripStore(Protocol):
    """Persistence for trips, keyed by slug."""

    async def list(self) -> list[TripSummary]:
        """All trips, most recently edited first. Summaries only — never full geometry."""
        ...

    async def get(self, slug: str) -> Trip:
        """Raises TripNotFound if absent."""
        ...

    async def create(self, trip: Trip) -> Trip:
        """Raises TripAlreadyExists if the slug is taken."""
        ...

    async def put(self, trip: Trip) -> Trip:
        """Create or replace."""
        ...

    async def delete(self, slug: str) -> None:
        """Raises TripNotFound if absent."""
        ...

    async def exists(self, slug: str) -> bool: ...


class InMemoryTripStore:
    """Non-durable store for development and tests.

    Trips are immutable models, so storing the object directly is safe — there is no
    reference a caller could mutate underneath us.
    """

    def __init__(self) -> None:
        self._trips: dict[str, Trip] = {}

    async def list(self) -> list[TripSummary]:
        return [
            TripSummary.from_trip(trip)
            for trip in sorted(self._trips.values(), key=lambda t: t.edited_at, reverse=True)
        ]

    async def get(self, slug: str) -> Trip:
        try:
            return self._trips[slug]
        except KeyError:
            raise TripNotFound(slug) from None

    async def create(self, trip: Trip) -> Trip:
        if trip.slug in self._trips:
            raise TripAlreadyExists(trip.slug)
        self._trips[trip.slug] = trip
        return trip

    async def put(self, trip: Trip) -> Trip:
        self._trips[trip.slug] = trip
        return trip

    async def delete(self, slug: str) -> None:
        if slug not in self._trips:
            raise TripNotFound(slug)
        del self._trips[slug]

    async def exists(self, slug: str) -> bool:
        return slug in self._trips
