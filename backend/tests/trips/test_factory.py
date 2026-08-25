"""Wiring trip storage from settings.

The default has to be safe in both directions: no bucket configured must not silently
write to a disk Cloud Run throws away, and a bucket configured must not silently fall back
to memory.
"""

import pytest

from motorooter.trips.errors import TripStorageConfigError
from motorooter.trips.factory import TripStorageSettings, build_trip_store, settings_from_env
from motorooter.trips.gcs import (
    AnonymousTokenSource,
    GcsObjectStore,
    MetadataServerTokenSource,
    StaticTokenSource,
)
from motorooter.trips.store import GcsTripStore, InMemoryTripStore


def test_no_bucket_gives_the_in_memory_store():
    assert isinstance(build_trip_store(TripStorageSettings()), InMemoryTripStore)


def test_a_bucket_gives_the_cloud_storage_store():
    store = build_trip_store(TripStorageSettings(bucket="motorooter-trips"))
    assert isinstance(store, GcsTripStore)


def token_source_for(settings: TripStorageSettings) -> object:
    store = build_trip_store(settings)
    assert isinstance(store, GcsTripStore)
    objects = store.objects
    assert isinstance(objects, GcsObjectStore)
    return objects.token_source


def test_cloud_storage_store_uses_ambient_credentials_by_default():
    """On Cloud Run the service account comes from the metadata server, not a key file."""
    source = token_source_for(TripStorageSettings(bucket="motorooter-trips"))
    assert isinstance(source, MetadataServerTokenSource)


def test_an_explicit_token_overrides_the_metadata_server():
    source = token_source_for(
        TripStorageSettings(bucket="motorooter-trips", access_token="local-dev-token")
    )
    assert isinstance(source, StaticTokenSource)


def test_an_emulator_needs_no_credentials():
    """A fake-gcs-server on localhost has no metadata server to ask."""
    source = token_source_for(
        TripStorageSettings(bucket="motorooter-trips", base_url="http://localhost:4443")
    )
    assert isinstance(source, AnonymousTokenSource)


def test_a_bucket_name_that_is_not_a_bucket_name_fails_at_startup():
    """Misconfiguration should fail the deploy, not the first save."""
    with pytest.raises(TripStorageConfigError):
        build_trip_store(TripStorageSettings(bucket="gs://motorooter-trips"))


def test_an_empty_prefix_is_refused():
    """Trips at the bucket root would collide with anything else stored there."""
    with pytest.raises(TripStorageConfigError):
        build_trip_store(TripStorageSettings(bucket="motorooter-trips", prefix=""))


class TestSettingsFromEnv:
    def test_defaults_to_no_bucket(self, monkeypatch):
        monkeypatch.delenv("MOTOROOTER_TRIPS_BUCKET", raising=False)
        assert settings_from_env().bucket is None

    def test_reads_the_bucket(self, monkeypatch):
        monkeypatch.setenv("MOTOROOTER_TRIPS_BUCKET", "motorooter-trips")
        assert settings_from_env().bucket == "motorooter-trips"

    def test_offline_ignores_a_configured_bucket(self, monkeypatch):
        """`MOTOROOTER_OFFLINE=1` must need no credentials and touch no external service."""
        monkeypatch.setenv("MOTOROOTER_TRIPS_BUCKET", "motorooter-trips")
        monkeypatch.setenv("MOTOROOTER_OFFLINE", "1")
        assert settings_from_env().bucket is None
