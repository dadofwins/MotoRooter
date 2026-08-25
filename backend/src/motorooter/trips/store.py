"""Trip persistence.

`TripStore` is the seam between the API and wherever trips actually live. The in-memory
implementation covers development and tests; the Cloud Storage implementation writes
`trips/<slug>/trip.json` and must pass the same contract suite
(`tests/trips/store_contract.py`) unchanged.

Note for any persistent implementation: Cloud Run's container filesystem is ephemeral and
per-instance, so trips must go to a bucket, never to local disk.
"""

import asyncio
import contextlib
import json
from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from motorooter.trips.errors import (
    TripAlreadyExists,
    TripDocumentInvalid,
    TripNotFound,
    TripStorageUnavailable,
)
from motorooter.trips.models import CURRENT_SCHEMA_VERSION, Trip, TripSummary
from motorooter.trips.objects import (
    ObjectAlreadyExists,
    ObjectNotFound,
    ObjectStore,
    ObjectStoreUnavailable,
)
from motorooter.trips.slug import validate_slug

DEFAULT_TRIP_PREFIX = "trips"

TRIP_DOCUMENT = "{prefix}/{slug}/trip.json"
"""One object per trip. The per-slug directory leaves room for exports beside it later."""


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


class GcsTripStore:
    """Durable trips, one JSON document per slug, in an object store.

    Dumb on purpose. It serializes what it is handed and refuses to clobber on `create`;
    it does not stamp timestamps, assign slugs, or enforce business rules. That belongs to
    the API layer, which is the only place that knows whether a write is a user edit.

    Two things it does own, because they are properties of *storage*:

    - **Slug validation.** A slug is concatenated into an object path here, so it is
      untrusted input crossing into a path context. Validating at the boundary that builds
      the path means no caller can forget to.
    - **Refusing to clobber atomically.** `create` writes with an if-absent precondition
      rather than checking `exists` first. Trips are public and world-editable, so two
      clients creating the same slug concurrently is ordinary, and a check-then-write loses
      one of them without either being told.

    There is no write-then-swap, and there should not be: object writes are already atomic,
    so a concurrent reader sees the previous document or the new one and never a splice.
    A temp-object-then-rename pattern would be strictly worse here, since object stores
    have no rename and the FUSE layer emulates it with a copy plus a delete.
    """

    def __init__(self, objects: ObjectStore, *, prefix: str = DEFAULT_TRIP_PREFIX) -> None:
        self._objects = objects
        self._prefix = prefix.strip("/")

    @property
    def objects(self) -> ObjectStore:
        return self._objects

    async def list(self) -> list[TripSummary]:
        """Read every trip document and summarize.

        A fan-out read per trip, which is honest but not free. The alternative — a manifest
        object holding the summaries — makes listing one request, at the cost of a second
        source of truth that concurrent writers can leave stale. At prototype scale the
        drift is the more expensive problem, so the index is derived, never stored.
        """
        with self._translating(""):
            paths = await self._objects.list_prefix(f"{self._prefix}/")

        documents = [(path, slug) for path in paths if (slug := self._slug_in(path)) is not None]
        trips = await asyncio.gather(*(self._read_for_listing(p, s) for p, s in documents))
        return sorted(
            (TripSummary.from_trip(trip) for trip in trips if trip is not None),
            key=lambda summary: summary.edited_at,
            reverse=True,
        )

    async def get(self, slug: str) -> Trip:
        path = self._path(slug)
        with self._translating(slug):
            data = await self._objects.read(path)
        return self._decode(data, slug)

    async def create(self, trip: Trip) -> Trip:
        path = self._path(trip.slug)
        with self._translating(trip.slug):
            await self._objects.write(path, self._encode(trip), if_absent=True)
        return trip

    async def put(self, trip: Trip) -> Trip:
        path = self._path(trip.slug)
        with self._translating(trip.slug):
            await self._objects.write(path, self._encode(trip))
        return trip

    async def delete(self, slug: str) -> None:
        path = self._path(slug)
        with self._translating(slug):
            await self._objects.delete(path)

    async def exists(self, slug: str) -> bool:
        path = self._path(slug)
        with self._translating(slug):
            return await self._objects.exists(path)

    # -- paths ---------------------------------------------------------------------------

    def _path(self, slug: str) -> str:
        return TRIP_DOCUMENT.format(prefix=self._prefix, slug=validate_slug(slug))

    def _slug_in(self, path: str) -> str | None:
        """The slug a listed object belongs to, or `None` if it is not a trip document.

        Anything else under the prefix — a stray file, a future export — is ignored rather
        than treated as a trip and failed on.
        """
        if not path.startswith(f"{self._prefix}/"):
            return None
        slug, separator, remainder = path[len(self._prefix) + 1 :].partition("/")
        if not separator or remainder != "trip.json" or not slug:
            return None
        return slug

    # -- serialization -------------------------------------------------------------------

    @staticmethod
    def _encode(trip: Trip) -> bytes:
        # Pydantic's own JSON serializer, not `json.dumps(model_dump())`: it is what round
        # trips tuples, enums, and aware datetimes back into the same types. Unindented
        # because a routed leg is thousands of coordinates and pretty-printing each onto
        # its own lines multiplies the object size for no reader's benefit.
        return trip.model_dump_json().encode()

    @staticmethod
    def _decode(data: bytes, slug: str) -> Trip:
        try:
            payload = json.loads(data)
        except ValueError as exc:
            raise TripDocumentInvalid(slug, f"not valid JSON ({exc})") from exc

        version = payload.get("schema_version") if isinstance(payload, dict) else None
        if isinstance(version, int) and version > CURRENT_SCHEMA_VERSION:
            # Refuse rather than parse what we can. Dropping fields this build cannot model
            # and then writing the result back would quietly destroy the user's trip.
            reason = (
                f"written at schema_version {version}, but this build understands at most "
                f"{CURRENT_SCHEMA_VERSION}"
            )
            raise TripDocumentInvalid(slug, reason)

        try:
            return Trip.model_validate(payload)
        except ValidationError as exc:
            # Translated, not propagated: a pydantic error escaping the store would make
            # the Cloud Storage implementation behave differently from the in-memory one.
            raise TripDocumentInvalid(slug, f"does not match the trip schema ({exc})") from exc

    async def _read_for_listing(self, path: str, slug: str) -> Trip | None:
        """Load one trip for the index, tolerating it disappearing underneath us.

        A listing is a snapshot: another client deleting a trip between the list call and
        this read is legitimate, and must not fail the whole index. A *corrupt* document is
        not tolerated — omitting it would be indistinguishable from the trip not existing,
        which is the one outcome a user cannot debug.
        """
        try:
            with self._translating(slug):
                data = await self._objects.read(path)
        except TripNotFound:
            return None
        return self._decode(data, slug)

    # -- error translation ---------------------------------------------------------------

    @staticmethod
    @contextlib.contextmanager
    def _translating(slug: str) -> Iterator[None]:
        """Object-store failures become the trip errors the API already maps to statuses."""
        try:
            yield
        except ObjectNotFound:
            raise TripNotFound(slug) from None
        except ObjectAlreadyExists:
            raise TripAlreadyExists(slug) from None
        except ObjectStoreUnavailable as exc:
            raise TripStorageUnavailable(str(exc)) from exc
