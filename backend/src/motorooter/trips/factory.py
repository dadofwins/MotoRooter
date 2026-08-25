"""Assembles trip storage from settings.

The mirror of `routing.factory`: the single place that names a concrete backing store, so
everything downstream depends only on the `TripStore` protocol.

The default is in-memory, which is right for tests and `MOTOROOTER_OFFLINE=1` and wrong for
production. Cloud Run must set `MOTOROOTER_TRIPS_BUCKET` — its filesystem is ephemeral and
per-instance, so an unconfigured deploy loses every trip on the next revision and shows
sibling instances different data in the meantime.
"""

import dataclasses
import os
import re

from motorooter.trips.errors import TripStorageConfigError
from motorooter.trips.gcs import (
    GCS_BASE_URL,
    AccessTokenSource,
    AnonymousTokenSource,
    GcsObjectStore,
    MetadataServerTokenSource,
    StaticTokenSource,
)
from motorooter.trips.store import DEFAULT_TRIP_PREFIX, GcsTripStore, InMemoryTripStore, TripStore

_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
"""Cloud Storage bucket naming rules, narrowed. Notably excludes a `gs://` scheme."""


@dataclasses.dataclass(frozen=True)
class TripStorageSettings:
    """Everything trip persistence needs to wire itself up."""

    bucket: str | None = None
    """Cloud Storage bucket. `None` selects the non-durable in-memory store."""

    prefix: str = DEFAULT_TRIP_PREFIX
    """Object-name prefix, so one bucket can hold several environments."""

    base_url: str = GCS_BASE_URL
    """Point at a storage emulator for local development."""

    access_token: str | None = None
    """Explicit bearer token, bypassing the metadata server. Local development only."""


def settings_from_env() -> TripStorageSettings:
    """Read storage config from the environment.

    `MOTOROOTER_OFFLINE=1` forces the in-memory store, matching what it already does to
    routing: the whole app runs with no credentials and touches no external service.
    """
    if os.environ.get("MOTOROOTER_OFFLINE") == "1":
        return TripStorageSettings()

    return TripStorageSettings(
        bucket=os.environ.get("MOTOROOTER_TRIPS_BUCKET") or None,
        prefix=os.environ.get("MOTOROOTER_TRIPS_PREFIX", DEFAULT_TRIP_PREFIX),
        base_url=os.environ.get("MOTOROOTER_GCS_BASE_URL", GCS_BASE_URL),
        access_token=os.environ.get("MOTOROOTER_GCS_ACCESS_TOKEN") or None,
    )


def build_trip_store(settings: TripStorageSettings) -> TripStore:
    """Build the trip store.

    Raises:
        TripStorageConfigError: the bucket or prefix cannot be addressed. Raised at startup
            so a typo fails the deploy rather than the first save.
    """
    if settings.bucket is None:
        return InMemoryTripStore()

    if not _BUCKET_NAME.match(settings.bucket):
        msg = (
            f"{settings.bucket!r} is not a Cloud Storage bucket name — expected the bare "
            "name, with no gs:// scheme, path, or uppercase letters"
        )
        raise TripStorageConfigError(msg)

    prefix = settings.prefix.strip("/")
    if not prefix:
        msg = "trip prefix must not be empty: trips at the bucket root collide with everything else"
        raise TripStorageConfigError(msg)

    objects = GcsObjectStore(
        bucket=settings.bucket,
        base_url=settings.base_url,
        token_source=_token_source(settings),
    )
    return GcsTripStore(objects, prefix=prefix)


def _token_source(settings: TripStorageSettings) -> AccessTokenSource:
    if settings.access_token is not None:
        return StaticTokenSource(settings.access_token)
    if settings.base_url != GCS_BASE_URL:
        # An emulator has no metadata server to ask, and asking would hang until timeout.
        return AnonymousTokenSource()
    return MetadataServerTokenSource()
