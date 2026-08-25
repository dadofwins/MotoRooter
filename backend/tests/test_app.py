"""How the application picks its trip store.

`MOTOROOTER_OFFLINE=1` has to mean *no external services*, not just no routing keys. A
store quietly reading a bucket name out of the ambient environment would make the test
suite depend on the developer's shell, which is exactly what offline mode exists to
prevent.
"""

import pytest

from motorooter.app import create_app
from motorooter.routing.factory import RoutingSettings
from motorooter.trips.errors import TripStorageConfigError
from motorooter.trips.factory import TripStorageSettings
from motorooter.trips.gcs import GcsObjectStore
from motorooter.trips.store import GcsTripStore, InMemoryTripStore

OFFLINE = RoutingSettings(offline=True)
LIVE = RoutingSettings(ors_api_key="ors-key", google_api_key="google-key")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("MOTOROOTER_OFFLINE", "MOTOROOTER_TRIPS_BUCKET", "MOTOROOTER_GCS_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_offline_uses_the_in_memory_store():
    assert isinstance(create_app(OFFLINE).state.trip_store, InMemoryTripStore)


def test_offline_ignores_a_bucket_in_the_environment(monkeypatch):
    """Otherwise a stray env var silently points the whole test suite at a real bucket."""
    monkeypatch.setenv("MOTOROOTER_TRIPS_BUCKET", "someones-real-bucket")
    assert isinstance(create_app(OFFLINE).state.trip_store, InMemoryTripStore)


def test_a_configured_bucket_is_used_when_not_offline(monkeypatch):
    monkeypatch.setenv("MOTOROOTER_TRIPS_BUCKET", "motorooter-trips")
    assert isinstance(create_app(LIVE).state.trip_store, GcsTripStore)


def test_real_routing_with_throwaway_trips_starts(monkeypatch):
    """The local-development shape, and the reason the two flags were split.

    Judging whether a route looks right needs real engines — fake straight-line geometry
    tells you nothing — but there is no bucket on a laptop.
    """
    monkeypatch.setenv("MOTOROOTER_TRIPS_EPHEMERAL", "1")
    app = create_app(LIVE)
    assert isinstance(app.state.trip_store, InMemoryTripStore)
    assert "fake" not in app.state.provider_registry.names()


def test_no_bucket_and_not_offline_refuses_to_start():
    """Better a failed deploy than a revision that comes up healthy and loses every trip."""
    with pytest.raises(TripStorageConfigError):
        create_app(LIVE)


def test_explicit_storage_settings_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("MOTOROOTER_TRIPS_BUCKET", "from-the-environment")
    app = create_app(LIVE, storage_settings=TripStorageSettings(bucket="explicitly-passed"))
    store = app.state.trip_store
    assert isinstance(store, GcsTripStore)
    objects = store.objects
    assert isinstance(objects, GcsObjectStore)
    assert objects.bucket == "explicitly-passed"


def test_an_injected_store_wins_over_everything(monkeypatch):
    monkeypatch.setenv("MOTOROOTER_TRIPS_BUCKET", "motorooter-trips")
    injected = InMemoryTripStore()
    assert create_app(LIVE, trip_store=injected).state.trip_store is injected
