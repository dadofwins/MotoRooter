"""Wiring trip storage from settings.

The default has to be safe in both directions: no bucket configured must not silently
write to a disk Cloud Run throws away, and a bucket configured must not silently fall back
to memory.
"""

import pytest

from motorooter.app import routing_settings_from_env
from motorooter.trips.errors import TripStorageConfigError
from motorooter.trips.factory import TripStorageSettings, build_trip_store, settings_from_env
from motorooter.trips.gcs import (
    GCS_BASE_URL,
    AnonymousTokenSource,
    GcsObjectStore,
    MetadataServerTokenSource,
    StaticTokenSource,
)
from motorooter.trips.store import GcsTripStore, InMemoryTripStore


def test_ephemeral_gives_the_in_memory_store():
    assert isinstance(build_trip_store(TripStorageSettings(ephemeral=True)), InMemoryTripStore)


def test_no_bucket_and_not_offline_fails_the_deploy():
    """A Cloud Run revision that forgets the bucket must not come up healthy and lossy.

    It would serve different trips per instance and lose all of them on the next
    revision. `MOTOROOTER_OFFLINE=1` is the explicit opt-in for running without durability.
    """
    with pytest.raises(TripStorageConfigError, match="MOTOROOTER_TRIPS_BUCKET"):
        build_trip_store(TripStorageSettings())


def test_ephemeral_ignores_a_bucket_that_is_also_configured():
    store = build_trip_store(TripStorageSettings(bucket="motorooter-trips", ephemeral=True))
    assert isinstance(store, InMemoryTripStore)


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


def test_a_trailing_slash_on_the_production_url_still_uses_real_credentials():
    """The adapter rstrips the base URL, so comparing the raw string dropped the token.

    The result was every request going to real GCS unauthenticated: 401, then 503 on every
    trip endpoint, from a config typo that startup validation could not see.
    """
    source = token_source_for(
        TripStorageSettings(bucket="motorooter-trips", base_url=f"{GCS_BASE_URL}/")
    )
    assert isinstance(source, MetadataServerTokenSource)


def test_the_store_shares_one_http_client_across_requests():
    """Listing fans out a read per trip; a client each makes that N TLS handshakes."""
    store = build_trip_store(TripStorageSettings(bucket="motorooter-trips"))
    assert isinstance(store, GcsTripStore)
    objects = store.objects
    assert isinstance(objects, GcsObjectStore)
    assert objects.client is not None


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
        monkeypatch.delenv("MOTOROOTER_OFFLINE", raising=False)
        assert settings_from_env().bucket is None

    def test_reads_the_bucket(self, monkeypatch):
        monkeypatch.setenv("MOTOROOTER_TRIPS_BUCKET", "motorooter-trips")
        assert settings_from_env().bucket == "motorooter-trips"

    def test_offline_still_implies_ephemeral_storage(self, monkeypatch):
        """`MOTOROOTER_OFFLINE=1` means no external services at all, storage included."""
        monkeypatch.setenv("MOTOROOTER_TRIPS_BUCKET", "motorooter-trips")
        monkeypatch.setenv("MOTOROOTER_OFFLINE", "1")
        assert settings_from_env().ephemeral is True
        assert isinstance(build_trip_store(settings_from_env()), InMemoryTripStore)

    def test_ephemeral_storage_can_be_opted_into_on_its_own(self, monkeypatch):
        """Real routing with throwaway trips: the local-development case.

        "I accept fake routing" and "I accept losing my trips" are different decisions, and
        one flag expressing both meant testing against real roads demanded a bucket that
        does not exist locally.
        """
        monkeypatch.delenv("MOTOROOTER_OFFLINE", raising=False)
        monkeypatch.setenv("MOTOROOTER_TRIPS_EPHEMERAL", "1")
        settings = settings_from_env()
        assert settings.ephemeral is True
        assert isinstance(build_trip_store(settings), InMemoryTripStore)

    def test_ephemeral_storage_does_not_imply_fake_routing(self, monkeypatch):
        """The whole point: the two opt-outs are independent."""
        monkeypatch.delenv("MOTOROOTER_OFFLINE", raising=False)
        monkeypatch.setenv("MOTOROOTER_TRIPS_EPHEMERAL", "1")
        assert routing_settings_from_env().offline is False

    def test_neither_opt_out_still_demands_a_bucket(self, monkeypatch):
        """The finding that stops a Cloud Run deploy coming up healthy and lossy."""
        monkeypatch.delenv("MOTOROOTER_OFFLINE", raising=False)
        monkeypatch.delenv("MOTOROOTER_TRIPS_EPHEMERAL", raising=False)
        monkeypatch.delenv("MOTOROOTER_TRIPS_BUCKET", raising=False)
        with pytest.raises(TripStorageConfigError, match="MOTOROOTER_TRIPS_BUCKET"):
            build_trip_store(settings_from_env())

    def test_the_error_names_both_ways_out(self, monkeypatch):
        """A deploy that hits this needs to know which opt-out it actually wants."""
        monkeypatch.delenv("MOTOROOTER_OFFLINE", raising=False)
        monkeypatch.delenv("MOTOROOTER_TRIPS_EPHEMERAL", raising=False)
        monkeypatch.delenv("MOTOROOTER_TRIPS_BUCKET", raising=False)
        with pytest.raises(TripStorageConfigError) as caught:
            build_trip_store(settings_from_env())
        assert "MOTOROOTER_TRIPS_EPHEMERAL" in str(caught.value)
