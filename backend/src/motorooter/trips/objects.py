"""The blob-storage seam.

`ObjectStore` is a deliberately small interface — five operations over opaque bytes — that
sits between `GcsTripStore` and Cloud Storage. Splitting it out buys three things:

- the trip store's serialization can be tested through real JSON bytes without HTTP;
- the Cloud Storage adapter can be tested for URL encoding, preconditions, and pagination
  without knowing what a trip is;
- swapping the backing store (a GCS FUSE mount, an emulator, a different cloud) costs an
  adapter rather than a rewrite, exactly as `RoutingProvider` does for engines.

The one non-obvious part is the `if_generation_match` precondition on `write`. Concurrency
control has to be a property of the *write*, not a read followed by a write: everything here
is public and unauthenticated, so two clients touching one slug at the same time is ordinary,
and a check-then-write leaves a window in which both see the same state and one silently
loses their edit.

There is exactly one mutating primitive with exactly one precondition, rather than a boolean
for creates and something else for updates. Two mechanisms for one job is how they drift.

    write(path, data)                                     # unconditional; last writer wins
    write(path, data, if_generation_match=MUST_NOT_EXIST) # create, refusing to clobber
    write(path, data, if_generation_match=n)              # replace only if unchanged since n

The third form is what makes read-merge-write safe. Without it, two clients each editing a
different field of the same document do not merely lose one write — the loser's fields are
rolled back to the state both of them read, and both clients are told they succeeded.
"""

import dataclasses
from typing import Protocol, runtime_checkable

MUST_NOT_EXIST = 0
"""Precondition sentinel: the write succeeds only if no object is live at that path.

Zero rather than a separate flag because that is what object stores actually implement — a
generation of 0 means "no live object" — so the seam expresses the primitive rather than a
translation of it.
"""


@dataclasses.dataclass(frozen=True)
class StoredObject:
    """An object's bytes and the version they were read at."""

    data: bytes
    generation: int
    """Opaque version, monotonic per object. Pass back as `if_generation_match` to write
    only if nothing has changed since this read."""


class ObjectStoreError(Exception):
    """Base for blob-storage failures. Translated to `TripError` by the trip store."""


class ObjectNotFound(ObjectStoreError):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"no object at {path!r}")


class ObjectAlreadyExists(ObjectStoreError):
    """A create lost the race, or the object was already there."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"an object already exists at {path!r}")


class ObjectVersionMismatch(ObjectStoreError):
    """The object changed between the caller's read and its write.

    Distinct from `ObjectAlreadyExists` because callers answer them differently: one means
    "pick another name", the other means "re-read and merge again".
    """

    def __init__(self, path: str, expected: int) -> None:
        self.path = path
        self.expected = expected
        super().__init__(f"object at {path!r} is no longer at generation {expected}")


class ObjectStoreUnavailable(ObjectStoreError):
    """The backing store could not be reached, or refused for a reason we cannot act on."""


@runtime_checkable
class ObjectStore(Protocol):
    """Bytes keyed by path. Writes are atomic: a reader never sees a partial object."""

    async def read(self, path: str) -> StoredObject:
        """Bytes plus the generation they were read at.

        Raises:
            ObjectNotFound: nothing is stored at that path.
        """
        ...

    async def exists(self, path: str) -> bool:
        """Presence without transferring the body."""
        ...

    async def write(self, path: str, data: bytes, *, if_generation_match: int | None = None) -> int:
        """Create or replace, returning the new generation.

        Args:
            if_generation_match: `None` writes unconditionally. `MUST_NOT_EXIST` creates,
                failing if anything is already there. Any other value replaces only if the
                object is still at that generation. Evaluated by the store, so there is no
                check-then-write window.

        Raises:
            ObjectAlreadyExists: `MUST_NOT_EXIST` was requested and something was there.
            ObjectVersionMismatch: a generation was requested and the object is not at it,
                including because it has since been deleted.
        """
        ...

    async def delete(self, path: str) -> None:
        """Raises ObjectNotFound if absent."""
        ...

    async def list_prefix(self, prefix: str) -> list[str]:
        """Every object path beginning with `prefix`, in no guaranteed order."""
        ...


class InMemoryObjectStore:
    """Non-durable object store for development and tests.

    Byte strings are immutable, so storing what the caller handed over is safe; there is no
    aliasing hazard of the kind a mutable buffer would create.
    """

    def __init__(self) -> None:
        self._objects: dict[str, StoredObject] = {}
        # Shared across paths rather than per-path, so a generation is never reused by a
        # different object and a stale version can never accidentally match.
        self._next_generation = 1

    async def read(self, path: str) -> StoredObject:
        try:
            return self._objects[path]
        except KeyError:
            raise ObjectNotFound(path) from None

    async def exists(self, path: str) -> bool:
        return path in self._objects

    async def write(self, path: str, data: bytes, *, if_generation_match: int | None = None) -> int:
        self._check_precondition(path, if_generation_match)
        generation = self._next_generation
        self._next_generation += 1
        self._objects[path] = StoredObject(data=data, generation=generation)
        return generation

    def _check_precondition(self, path: str, expected: int | None) -> None:
        if expected is None:
            return
        current = self._objects.get(path)
        if expected == MUST_NOT_EXIST:
            if current is not None:
                raise ObjectAlreadyExists(path)
            return
        # A missing object is a mismatch, not a create: it was deleted under the caller,
        # and silently recreating it would resurrect data someone chose to remove.
        if current is None or current.generation != expected:
            raise ObjectVersionMismatch(path, expected)

    async def delete(self, path: str) -> None:
        if path not in self._objects:
            raise ObjectNotFound(path)
        del self._objects[path]

    async def list_prefix(self, prefix: str) -> list[str]:
        return [path for path in self._objects if path.startswith(prefix)]
