"""Assembles trip storage from settings.

The mirror of `routing.factory`: the single place that names a concrete backing store, so
everything downstream depends only on the `TripStore` protocol.

Durability is required unless explicitly waived. A deploy with neither
`MOTOROOTER_TRIPS_BUCKET` nor `MOTOROOTER_OFFLINE=1` fails to start rather than coming up
healthy: Cloud Run's filesystem is ephemeral and per-instance, so the in-memory store there
would show sibling instances different data and lose all of it on the next revision. That is
a failure nobody notices until they look for a trip that is gone, which makes it exactly the
kind of misconfiguration that should fail the deploy instead.
"""

import dataclasses
import os
import re

import httpx

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

    offline: bool = False
    """Run with no durable storage and no credentials. The explicit opt-out of persistence."""

    @property
    def normalized_base_url(self) -> str:
        """Trailing slashes removed, so comparisons agree with what the adapter uses.

        `GcsObjectStore` rstrips its base URL. Comparing the raw string here meant
        `https://storage.googleapis.com/` did not match the production constant, so the
        factory silently chose anonymous credentials while still pointing at real GCS.
        """
        return self.base_url.rstrip("/")


def settings_from_env() -> TripStorageSettings:
    """Read storage config from the environment.

    `MOTOROOTER_OFFLINE=1` forces the in-memory store, matching what it already does to
    routing: the whole app runs with no credentials and touches no external service.
    """
    return TripStorageSettings(
        offline=os.environ.get("MOTOROOTER_OFFLINE") == "1",
        bucket=os.environ.get("MOTOROOTER_TRIPS_BUCKET") or None,
        prefix=os.environ.get("MOTOROOTER_TRIPS_PREFIX", DEFAULT_TRIP_PREFIX),
        base_url=os.environ.get("MOTOROOTER_GCS_BASE_URL", GCS_BASE_URL),
        access_token=os.environ.get("MOTOROOTER_GCS_ACCESS_TOKEN") or None,
    )


def build_trip_store(settings: TripStorageSettings) -> TripStore:
    """Build the trip store.

    Raises:
        TripStorageConfigError: no bucket and not offline, or the bucket or prefix cannot be
            addressed. Raised at startup so a typo fails the deploy rather than the first
            save of the day.
    """
    if settings.offline:
        return InMemoryTripStore()

    if settings.bucket is None:
        msg = (
            "MOTOROOTER_TRIPS_BUCKET is required: without it trips would be held in memory, "
            "which on Cloud Run means each instance serving different data and all of it "
            "lost on the next revision. Set MOTOROOTER_OFFLINE=1 to run without durability "
            "on purpose."
        )
        raise TripStorageConfigError(msg)

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

    # One client, one connection pool. Listing fans out a read per trip, and a client per
    # request would make that N TLS handshakes rather than N requests over one connection.
    client = httpx.AsyncClient()
    objects = GcsObjectStore(
        bucket=settings.bucket,
        base_url=settings.normalized_base_url,
        token_source=_token_source(settings, client),
        client=client,
    )
    return GcsTripStore(objects, prefix=prefix)


def _token_source(settings: TripStorageSettings, client: httpx.AsyncClient) -> AccessTokenSource:
    if settings.access_token is not None:
        return StaticTokenSource(settings.access_token)
    if settings.normalized_base_url != GCS_BASE_URL:
        # An emulator has no metadata server to ask, and asking would hang until timeout.
        return AnonymousTokenSource()
    return MetadataServerTokenSource(client=client)
