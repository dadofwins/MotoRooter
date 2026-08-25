"""In-memory object store, verified against the shared object-store contract."""

import pytest

from motorooter.trips.objects import InMemoryObjectStore
from tests.trips.object_store_contract import ObjectStoreContract


class TestInMemoryObjectStore(ObjectStoreContract):
    @pytest.fixture
    def objects(self):
        return InMemoryObjectStore()


async def test_stored_bytes_are_not_aliased():
    """Handing back the caller's buffer would let a later mutation rewrite history."""
    objects = InMemoryObjectStore()
    data = bytearray(b"original")
    await objects.write("trips/a/trip.json", bytes(data))
    data[:] = b"mutated!"
    assert (await objects.read("trips/a/trip.json")).data == b"original"
