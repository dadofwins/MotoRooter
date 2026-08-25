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
import dataclasses
import json
from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from motorooter.trips.errors import (
    TripAlreadyExists,
    TripDocumentInvalid,
    TripModifiedConcurrently,
    TripNotFound,
    TripStorageUnavailable,
)
from motorooter.trips.models import CURRENT_SCHEMA_VERSION, Trip, TripSummary
from motorooter.trips.objects import (
    MUST_NOT_EXIST,
    ObjectAlreadyExists,
    ObjectNotFound,
    ObjectStore,
    ObjectStoreUnavailable,
    ObjectVersionMismatch,
)
from motorooter.trips.slug import validate_slug

DEFAULT_TRIP_PREFIX = "trips"

TRIP_DOCUMENT = "{prefix}/{slug}/trip.json"
"""One object per trip. The per-slug directory leaves room for exports beside it later."""


@dataclasses.dataclass(frozen=True)
class VersionedTrip:
    """A trip and the version it was read at, for read-merge-write."""

    trip: Trip
    version: int


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

    async def get_versioned(self, slug: str) -> VersionedTrip:
        """The trip plus the version to pass back to `put`. Raises TripNotFound if absent."""
        ...

    async def put(self, trip: Trip, *, if_version: int | None = None) -> Trip:
        """Create or replace.

        Args:
            if_version: from `get_versioned`. Refuses the write if the trip has changed
                since. Omit for last-writer-wins.

        Raises:
            TripModifiedConcurrently: `if_version` was given and no longer matches.
        """
        ...

    async def delete(self, slug: str) -> None:
        """Raises TripNotFound if absent."""
        ...

    async def exists(self, slug: str) -> bool: ...


class InMemoryTripStore:
    """Non-durable store for development and tests.

    Trips are immutable models, so storing the object directly is safe — there is no
    reference a caller could mutate underneath us.

    Versioned like the durable store, deliberately. If only Cloud Storage enforced
    `if_version`, the API's conflict-and-retry path would be dead code in every test and
    live only in production, which is the worst place to first exercise it.
    """

    def __init__(self) -> None:
        self._trips: dict[str, Trip] = {}
        self._versions: dict[str, int] = {}
        self._next_version = 1

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

    async def get_versioned(self, slug: str) -> VersionedTrip:
        return VersionedTrip(trip=await self.get(slug), version=self._versions[slug])

    async def create(self, trip: Trip) -> Trip:
        if trip.slug in self._trips:
            raise TripAlreadyExists(trip.slug)
        return self._store(trip)

    async def put(self, trip: Trip, *, if_version: int | None = None) -> Trip:
        if if_version is not None and self._versions.get(trip.slug) != if_version:
            # Also covers the deleted case: no version means no trip, and recreating it
            # here would resurrect something somebody chose to remove.
            raise TripModifiedConcurrently(trip.slug, if_version)
        return self._store(trip)

    def _store(self, trip: Trip) -> Trip:
        self._trips[trip.slug] = trip
        self._versions[trip.slug] = self._next_version
        self._next_version += 1
        return trip

    async def delete(self, slug: str) -> None:
        if slug not in self._trips:
            raise TripNotFound(slug)
        del self._trips[slug]
        del self._versions[slug]

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
        with self._translating_storage_only():
            paths = await self._objects.list_prefix(f"{self._prefix}/")

        documents = [(path, slug) for path in paths if (slug := self._slug_in(path)) is not None]
        trips = await asyncio.gather(*(self._read_for_listing(p, s) for p, s in documents))
        return sorted(
            (TripSummary.from_trip(trip) for trip in trips if trip is not None),
            key=lambda summary: summary.edited_at,
            reverse=True,
        )

    async def get(self, slug: str) -> Trip:
        return (await self.get_versioned(slug)).trip

    async def get_versioned(self, slug: str) -> VersionedTrip:
        path = self._path(slug)
        with self._translating(slug):
            stored = await self._objects.read(path)
        return VersionedTrip(trip=self._decode(stored.data, slug), version=stored.generation)

    async def create(self, trip: Trip) -> Trip:
        path = self._path(trip.slug)
        with self._translating(trip.slug):
            await self._objects.write(path, self._encode(trip), if_generation_match=MUST_NOT_EXIST)
        return trip

    async def put(self, trip: Trip, *, if_version: int | None = None) -> Trip:
        path = self._path(trip.slug)
        with self._translating(trip.slug, expected_version=if_version):
            await self._objects.write(path, self._encode(trip), if_generation_match=if_version)
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

        # Checked before validating, because a future document may well have fields this
        # build cannot parse, and "written by a newer version" is the more useful of the
        # two errors.
        if isinstance(payload, dict):
            GcsTripStore._check_schema_version(payload.get("schema_version"), slug)

        try:
            trip = Trip.model_validate(payload)
        except ValidationError as exc:
            # Translated, not propagated: a pydantic error escaping the store would make
            # the Cloud Storage implementation behave differently from the in-memory one.
            raise TripDocumentInvalid(slug, f"does not match the trip schema ({exc})") from exc

        # And again on the parsed value: pydantic coerces `"99"` to `99`, so a raw check
        # alone would wave a future document through as if it were current.
        GcsTripStore._check_schema_version(trip.schema_version, slug)

        if trip.slug != slug:
            # Checked on every read, not just when listing. The document's slug is the
            # trip's identity to a client, so it cannot be taken from the body: a copied
            # object would report itself under the name it was copied from, and a body
            # carrying "../../admin" would reach the frontend unvalidated even though
            # `_path` would refuse to write it.
            reason = f"document claims slug {trip.slug!r} but is stored under {slug!r}"
            raise TripDocumentInvalid(slug, reason)
        return trip

    @staticmethod
    def _check_schema_version(version: object, slug: str) -> None:
        """Refuse a document from the future rather than parse what we can of it.

        Dropping fields this build cannot model and then writing the result back would
        quietly destroy the user's trip, and they would have no way to tell it happened.
        """
        if not isinstance(version, int) or isinstance(version, bool):
            return
        if version > CURRENT_SCHEMA_VERSION:
            reason = (
                f"written at schema_version {version}, but this build understands at most "
                f"{CURRENT_SCHEMA_VERSION}"
            )
            raise TripDocumentInvalid(slug, reason)

    async def _read_for_listing(self, path: str, slug: str) -> Trip | None:
        """Load one trip for the index, tolerating it disappearing underneath us.

        A listing is a snapshot: another client deleting a trip between the list call and
        this read is legitimate, and must not fail the whole index. A *corrupt* document is
        not tolerated — omitting it would be indistinguishable from the trip not existing,
        which is the one outcome a user cannot debug.
        """
        try:
            with self._translating(slug):
                stored = await self._objects.read(path)
        except TripNotFound:
            return None

        return self._decode(stored.data, slug)

    # -- error translation ---------------------------------------------------------------

    @staticmethod
    @contextlib.contextmanager
    def _translating(slug: str, *, expected_version: int | None = None) -> Iterator[None]:
        """Object-store failures become the trip errors the API already maps to statuses."""
        try:
            yield
        except ObjectNotFound:
            raise TripNotFound(slug) from None
        except ObjectAlreadyExists:
            raise TripAlreadyExists(slug) from None
        except ObjectVersionMismatch:
            raise TripModifiedConcurrently(slug, expected_version or 0) from None
        except ObjectStoreUnavailable as exc:
            raise TripStorageUnavailable(str(exc)) from exc

    @staticmethod
    @contextlib.contextmanager
    def _translating_storage_only() -> Iterator[None]:
        """For operations with no single slug to blame.

        Listing has no subject, so the slug-shaped errors do not apply: reporting a failed
        index as `TripNotFound("")` produced "no trip named ''" for `GET /api/trips`, which
        tells a client nothing true.
        """
        try:
            yield
        except ObjectStoreUnavailable as exc:
            raise TripStorageUnavailable(str(exc)) from exc
        except ObjectNotFound as exc:
            raise TripStorageUnavailable(f"trip index unavailable: {exc}") from exc
