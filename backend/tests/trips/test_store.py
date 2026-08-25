"""In-memory trip store, verified against the shared store contract."""

import pytest

from motorooter.trips.store import InMemoryTripStore, TripStore
from tests.trips.store_contract import TripStoreContract, TripStoreRoundTripContract


class TestInMemoryStore(TripStoreContract):
    @pytest.fixture
    def store(self):
        return InMemoryTripStore()


class TestInMemoryStoreRoundTrip(TripStoreRoundTripContract):
    @pytest.fixture
    def store(self):
        return InMemoryTripStore()


def test_satisfies_the_protocol():
    assert isinstance(InMemoryTripStore(), TripStore)
