"""The blob-storage seam.

`ObjectStore` is a deliberately small interface — five operations over opaque bytes — that
sits between `GcsTripStore` and Cloud Storage. Splitting it out buys three things:

- the trip store's serialization can be tested through real JSON bytes without HTTP;
- the Cloud Storage adapter can be tested for URL encoding, preconditions, and pagination
  without knowing what a trip is;
- swapping the backing store (a GCS FUSE mount, an emulator, a different cloud) costs an
  adapter rather than a rewrite, exactly as `RoutingProvider` does for engines.

The one non-obvious operation is `write(..., if_absent=True)`. Refusing to clobber has to
be a property of the *write*, not a read followed by a write: everything here is public and
unauthenticated, so two clients creating the same slug at the same time is an ordinary
event, and a check-then-write leaves a window in which both see "absent" and one silently
loses their trip.
"""

from typing import Protocol, runtime_checkable


class ObjectStoreError(Exception):
    """Base for blob-storage failures. Translated to `TripError` by the trip store."""


class ObjectNotFound(ObjectStoreError):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"no object at {path!r}")


class ObjectAlreadyExists(ObjectStoreError):
    """A conditional create lost the race, or the object was already there."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"an object already exists at {path!r}")


class ObjectStoreUnavailable(ObjectStoreError):
    """The backing store could not be reached, or refused for a reason we cannot act on."""


@runtime_checkable
class ObjectStore(Protocol):
    """Bytes keyed by path. Writes are atomic: a reader never sees a partial object."""

    async def read(self, path: str) -> bytes:
        """Raises ObjectNotFound if absent."""
        ...

    async def exists(self, path: str) -> bool:
        """Presence without transferring the body."""
        ...

    async def write(self, path: str, data: bytes, *, if_absent: bool = False) -> None:
        """Create or replace.

        Args:
            if_absent: fail rather than overwrite. Atomic — no check-then-write window.

        Raises:
            ObjectAlreadyExists: `if_absent` was set and the object was already there.
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
        self._objects: dict[str, bytes] = {}

    async def read(self, path: str) -> bytes:
        try:
            return self._objects[path]
        except KeyError:
            raise ObjectNotFound(path) from None

    async def exists(self, path: str) -> bool:
        return path in self._objects

    async def write(self, path: str, data: bytes, *, if_absent: bool = False) -> None:
        if if_absent and path in self._objects:
            raise ObjectAlreadyExists(path)
        self._objects[path] = data

    async def delete(self, path: str) -> None:
        if path not in self._objects:
            raise ObjectNotFound(path)
        del self._objects[path]

    async def list_prefix(self, prefix: str) -> list[str]:
        return [path for path in self._objects if path.startswith(prefix)]
