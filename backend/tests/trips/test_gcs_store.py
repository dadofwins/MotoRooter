"""Cloud Storage trip store.

Run against both seams. `InMemoryObjectStore` exercises the serialization and trip
semantics; the fake GCS JSON API runs the identical contract through real HTTP, so URL
encoding, generation preconditions, and pagination are covered by the same assertions
rather than by a parallel set that could drift.
"""

import asyncio

import pytest
import respx

from motorooter.trips.errors import (
    TripAlreadyExists,
    TripDocumentInvalid,
    TripNotFound,
    TripStorageUnavailable,
)
from motorooter.trips.gcs import GcsObjectStore, StaticTokenSource
from motorooter.trips.objects import (
    InMemoryObjectStore,
    ObjectStoreUnavailable,
    StoredObject,
)
from motorooter.trips.slug import InvalidSlug
from motorooter.trips.store import TRIP_DOCUMENT, GcsTripStore, TripStore
from tests.trips.fake_gcs import BASE_URL, BUCKET, FakeGcs, upload_count, written_trip
from tests.trips.store_contract import (
    TripStoreContract,
    TripStoreRoundTripContract,
    TripStoreVersioningContract,
    fully_populated_trip,
    make_trip,
)


@pytest.fixture
def objects():
    return InMemoryObjectStore()


@pytest.fixture
def store(objects):
    return GcsTripStore(objects)


@pytest.fixture
def fake_gcs():
    with respx.mock(assert_all_called=False) as mock:
        fake = FakeGcs()
        fake.install(mock)
        yield fake


@pytest.fixture
def gcs_store(fake_gcs):
    return GcsTripStore(
        GcsObjectStore(
            bucket=BUCKET, base_url=BASE_URL, token_source=StaticTokenSource("test-token")
        )
    )


class TestGcsTripStore(TripStoreContract):
    @pytest.fixture
    def store(self, objects):
        return GcsTripStore(objects)


class TestGcsTripStoreRoundTrip(TripStoreRoundTripContract):
    @pytest.fixture
    def store(self, objects):
        return GcsTripStore(objects)


class TestOverTheRealApiShape(TripStoreContract):
    """The same guarantees, end to end through the Cloud Storage JSON API."""

    @pytest.fixture
    def store(self, gcs_store):
        return gcs_store


class TestOverTheRealApiShapeRoundTrip(TripStoreRoundTripContract):
    @pytest.fixture
    def store(self, gcs_store):
        return gcs_store


class TestGcsTripStoreVersioning(TripStoreVersioningContract):
    @pytest.fixture
    def store(self, objects):
        return GcsTripStore(objects)


class TestOverTheRealApiShapeVersioning(TripStoreVersioningContract):
    """Optimistic concurrency end to end, including the 412 the JSON API actually returns."""

    @pytest.fixture
    def store(self, gcs_store):
        return gcs_store


def test_satisfies_the_protocol(objects):
    assert isinstance(GcsTripStore(objects), TripStore)


class TestObjectLayout:
    async def test_writes_one_document_per_slug(self, store, objects):
        await store.create(make_trip("oregon-backcountry"))
        assert await objects.list_prefix("") == ["trips/oregon-backcountry/trip.json"]

    async def test_prefix_is_configurable(self, objects):
        await GcsTripStore(objects, prefix="staging/trips").create(make_trip("a-trip"))
        assert await objects.list_prefix("") == ["staging/trips/a-trip/trip.json"]

    async def test_document_is_valid_json_with_the_slug_inside(self, gcs_store, fake_gcs):
        await gcs_store.create(make_trip("oregon-backcountry"))
        assert written_trip(fake_gcs, "oregon-backcountry")["slug"] == "oregon-backcountry"

    async def test_create_is_a_single_write(self, gcs_store, fake_gcs):
        """No write-then-swap: GCS object writes are already atomic, and it has no rename."""
        await gcs_store.create(make_trip("oregon-backcountry"))
        path = TRIP_DOCUMENT.format(prefix="trips", slug="oregon-backcountry")
        assert upload_count(fake_gcs, path) == 1
        assert set(fake_gcs.objects) == {"trips/oregon-backcountry/trip.json"}

    async def test_geometry_is_not_stored_as_a_nested_temp_object(self, gcs_store, fake_gcs):
        await gcs_store.put(fully_populated_trip())
        assert set(fake_gcs.objects) == {"trips/oregon-backcountry/trip.json"}


class InterleavingObjectStore(InMemoryObjectStore):
    """Yields control at every operation boundary, so two callers genuinely interleave.

    Without this the race tests are theatre: `asyncio.gather` over an in-process store runs
    each coroutine to completion before the next starts, and a check-then-write `create`
    passes them. Suspending at each boundary reproduces what two Cloud Run instances do to
    the same slug.
    """

    async def exists(self, path: str) -> bool:
        await asyncio.sleep(0)
        return await super().exists(path)

    async def write(
        self, path: str, data: bytes, *, if_generation_match: int | None = None
    ) -> int:
        await asyncio.sleep(0)
        return await super().write(path, data, if_generation_match=if_generation_match)

    async def read(self, path: str) -> StoredObject:
        await asyncio.sleep(0)
        return await super().read(path)


class TestConcurrentWriters:
    """Everything is public and unauthenticated, so two writers on one slug is expected."""

    @pytest.fixture
    def racing(self):
        return GcsTripStore(InterleavingObjectStore())

    async def test_only_one_of_two_racing_creates_wins(self, racing):
        results = await asyncio.gather(
            racing.create(make_trip("shared", name="First")),
            racing.create(make_trip("shared", name="Second")),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(failures) == 1
        assert isinstance(failures[0], TripAlreadyExists)

    async def test_the_loser_of_a_race_does_not_corrupt_the_winner(self, racing):
        await asyncio.gather(
            racing.create(make_trip("shared", name="First")),
            racing.create(make_trip("shared", name="Second")),
            return_exceptions=True,
        )
        assert (await racing.get("shared")).name in {"First", "Second"}

    async def test_a_create_racing_a_delete_never_resurrects_a_half_trip(self, racing):
        await racing.put(make_trip("shared", name="Original"))
        results = await asyncio.gather(
            racing.delete("shared"),
            racing.create(make_trip("shared", name="Recreated")),
            return_exceptions=True,
        )
        surviving = [r for r in results if isinstance(r, BaseException)]
        assert all(isinstance(r, TripAlreadyExists | TripNotFound) for r in surviving)

    async def test_racing_puts_leave_a_whole_document_not_a_torn_one(self, gcs_store):
        """Last writer wins by design; what must never happen is a half-written trip."""
        await asyncio.gather(
            *(gcs_store.put(fully_populated_trip()) for _ in range(5)),
        )
        reloaded = await gcs_store.get("oregon-backcountry")
        assert reloaded == fully_populated_trip()

    async def test_create_does_not_check_then_write(self, gcs_store, fake_gcs):
        """A read-then-write create has a window where both callers see 'absent'."""
        await gcs_store.create(make_trip("oregon-backcountry"))
        post = next(r for r in fake_gcs.requests if r.method == "POST")
        assert "ifGenerationMatch=0" in post.url.raw_path.decode()


class VanishingObjectStore(InMemoryObjectStore):
    """Another client deletes the doomed object just before we get to read it."""

    def __init__(self, *, doomed: str) -> None:
        super().__init__()
        self._doomed = doomed

    async def read(self, path: str) -> StoredObject:
        if self._doomed in path:
            await super().delete(path)
        return await super().read(path)


class TestListing:
    async def test_ignores_objects_that_are_not_trip_documents(self, store, objects):
        await store.create(make_trip("real-trip"))
        await objects.write("trips/real-trip/notes.txt", b"scratch")
        await objects.write("trips/stray.json", b"{}")
        assert [s.slug for s in await store.list()] == ["real-trip"]

    async def test_survives_a_trip_deleted_mid_listing(self):
        """A listing is a snapshot; another writer deleting a trip must not 404 the index."""
        store = GcsTripStore(VanishingObjectStore(doomed="vanishing"))
        await store.create(make_trip("kept"))
        await store.create(make_trip("vanishing"))
        assert [s.slug for s in await store.list()] == ["kept"]

    async def test_summaries_carry_totals_from_the_stored_geometry(self, store):
        await store.create(fully_populated_trip())
        summary = (await store.list())[0]
        assert summary.total_distance_m == pytest.approx(111_195.0)
        assert summary.needs_replan is False


class TestSlugIsAValidatedPathSegment:
    """Slugs become object paths, so the store validates them rather than trusting callers."""

    @pytest.mark.parametrize(
        "slug", ["../../etc/passwd", "a/b", "Upper", "", "trailing-", "with space"]
    )
    async def test_get_rejects_an_unsafe_slug(self, store, slug):
        with pytest.raises(InvalidSlug):
            await store.get(slug)

    @pytest.mark.parametrize("slug", ["../evil", "a/b"])
    async def test_exists_rejects_an_unsafe_slug(self, store, slug):
        with pytest.raises(InvalidSlug):
            await store.exists(slug)

    async def test_delete_rejects_an_unsafe_slug(self, store):
        with pytest.raises(InvalidSlug):
            await store.delete("../other-bucket-path")

    async def test_create_rejects_a_trip_whose_slug_is_unsafe(self, store, objects):
        trip = make_trip().model_copy(update={"slug": "../escape"})
        with pytest.raises(InvalidSlug):
            await store.create(trip)
        assert await objects.list_prefix("") == []

    async def test_put_rejects_a_trip_whose_slug_is_unsafe(self, store, objects):
        trip = make_trip().model_copy(update={"slug": "a/b"})
        with pytest.raises(InvalidSlug):
            await store.put(trip)
        assert await objects.list_prefix("") == []


class TestCorruptDocuments:
    async def test_unparseable_json_is_a_trip_error_not_a_json_error(self, store, objects):
        await objects.write("trips/broken/trip.json", b"{not json")
        with pytest.raises(TripDocumentInvalid):
            await store.get("broken")

    async def test_valid_json_that_is_not_a_trip_is_a_trip_error(self, store, objects):
        """A pydantic ValidationError escaping here would break store substitutability."""
        await objects.write("trips/broken/trip.json", b'{"slug": "broken"}')
        with pytest.raises(TripDocumentInvalid):
            await store.get("broken")

    async def test_a_newer_schema_version_is_refused_rather_than_guessed_at(
        self, store, objects
    ):
        """Silently dropping fields the running code cannot model would lose a user's trip."""
        document = make_trip().model_dump_json()
        future = document.replace('"schema_version":1', '"schema_version":99')
        await objects.write("trips/oregon-backcountry/trip.json", future.encode())
        with pytest.raises(TripDocumentInvalid) as caught:
            await store.get("oregon-backcountry")
        assert "99" in str(caught.value)

    async def test_a_newer_schema_version_written_as_a_string_is_also_refused(
        self, store, objects
    ):
        """Pydantic coerces `"99"` to `99`, so a raw pre-check alone would wave it through."""
        document = make_trip().model_dump_json()
        future = document.replace('"schema_version":1', '"schema_version":"99"')
        await objects.write("trips/oregon-backcountry/trip.json", future.encode())
        with pytest.raises(TripDocumentInvalid):
            await store.get("oregon-backcountry")

    async def test_a_document_stored_under_the_wrong_slug_is_refused(self, store, objects):
        """The body's slug is the trip's identity to a client, so it cannot be trusted.

        Copying trips/a/trip.json to trips/b/ would otherwise list two entries both claiming
        to be "a", and a body carrying "slug": "../../admin" would reach the frontend
        unvalidated even though `_path` would refuse to write it.
        """
        await store.create(make_trip("real-trip"))
        copied = (await objects.read("trips/real-trip/trip.json")).data
        await objects.write("trips/impostor/trip.json", copied)
        with pytest.raises(TripDocumentInvalid, match="impostor"):
            await store.list()

    async def test_a_document_claiming_a_traversal_slug_is_refused_on_read(self, store, objects):
        forged = make_trip().model_dump_json().replace(
            '"slug":"oregon-backcountry"', '"slug":"../../admin"'
        )
        await objects.write("trips/oregon-backcountry/trip.json", forged.encode())
        with pytest.raises(TripDocumentInvalid):
            await store.get("oregon-backcountry")

    async def test_a_corrupt_document_fails_the_listing_loudly(self, store, objects):
        """Quietly omitting it would look identical to the trip having been deleted."""
        await store.create(make_trip("fine"))
        await objects.write("trips/broken/trip.json", b"{not json")
        with pytest.raises(TripDocumentInvalid):
            await store.list()


class TestBackendFailures:
    """The object store's failures become trip errors the API already maps to 503."""

    @pytest.fixture
    def unavailable(self, objects):
        async def boom(*_args: object, **_kwargs: object) -> object:
            raise ObjectStoreUnavailable("bucket unreachable")

        for method in ("read", "write", "delete", "exists", "list_prefix"):
            setattr(objects, method, boom)
        return GcsTripStore(objects)

    async def test_get_translates(self, unavailable):
        with pytest.raises(TripStorageUnavailable):
            await unavailable.get("oregon-backcountry")

    async def test_create_translates(self, unavailable):
        with pytest.raises(TripStorageUnavailable):
            await unavailable.create(make_trip())

    async def test_put_translates(self, unavailable):
        with pytest.raises(TripStorageUnavailable):
            await unavailable.put(make_trip())

    async def test_delete_translates(self, unavailable):
        with pytest.raises(TripStorageUnavailable):
            await unavailable.delete("oregon-backcountry")

    async def test_exists_translates(self, unavailable):
        with pytest.raises(TripStorageUnavailable):
            await unavailable.exists("oregon-backcountry")

    async def test_list_translates(self, unavailable):
        with pytest.raises(TripStorageUnavailable):
            await unavailable.list()

    async def test_get_missing_still_reads_as_not_found(self, store):
        with pytest.raises(TripNotFound):
            await store.get("gone")
