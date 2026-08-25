"""Shared contract every `ObjectStore` must satisfy.

Subclass `ObjectStoreContract` and override the `objects` fixture. The in-memory fake and
the Cloud Storage adapter both pass this suite, which is what makes it safe to run the
trip-store contract against the fake and trust the result.

The precondition tests are the load-bearing ones: `write(if_absent=True)` is how
`GcsTripStore.create` refuses to clobber without a check-then-write race, and a fake that
gets those semantics wrong would hide the bug rather than expose it.
"""

import pytest

from motorooter.trips.objects import ObjectAlreadyExists, ObjectNotFound, ObjectStore


class ObjectStoreContract:
    @pytest.fixture
    def objects(self) -> ObjectStore:
        raise NotImplementedError("override the `objects` fixture")

    def test_satisfies_the_protocol(self, objects):
        assert isinstance(objects, ObjectStore)

    async def test_write_then_read_returns_the_bytes(self, objects):
        await objects.write("trips/a/trip.json", b'{"slug":"a"}')
        assert await objects.read("trips/a/trip.json") == b'{"slug":"a"}'

    async def test_read_missing_raises(self, objects):
        with pytest.raises(ObjectNotFound):
            await objects.read("trips/nope/trip.json")

    async def test_write_overwrites_by_default(self, objects):
        await objects.write("trips/a/trip.json", b"first")
        await objects.write("trips/a/trip.json", b"second")
        assert await objects.read("trips/a/trip.json") == b"second"

    async def test_write_if_absent_creates(self, objects):
        await objects.write("trips/a/trip.json", b"first", if_absent=True)
        assert await objects.read("trips/a/trip.json") == b"first"

    async def test_write_if_absent_refuses_to_overwrite(self, objects):
        await objects.write("trips/a/trip.json", b"first")
        with pytest.raises(ObjectAlreadyExists):
            await objects.write("trips/a/trip.json", b"second", if_absent=True)

    async def test_refused_write_leaves_the_original_intact(self, objects):
        """A rejected create must not be a partial write — readers see the old object."""
        await objects.write("trips/a/trip.json", b"first")
        with pytest.raises(ObjectAlreadyExists):
            await objects.write("trips/a/trip.json", b"second", if_absent=True)
        assert await objects.read("trips/a/trip.json") == b"first"

    async def test_exists_reports_presence(self, objects):
        await objects.write("trips/a/trip.json", b"x")
        assert await objects.exists("trips/a/trip.json") is True

    async def test_exists_reports_absence(self, objects):
        assert await objects.exists("trips/nope/trip.json") is False

    async def test_delete_removes_the_object(self, objects):
        await objects.write("trips/a/trip.json", b"x")
        await objects.delete("trips/a/trip.json")
        assert await objects.exists("trips/a/trip.json") is False

    async def test_delete_missing_raises(self, objects):
        with pytest.raises(ObjectNotFound):
            await objects.delete("trips/nope/trip.json")

    async def test_list_prefix_is_empty_initially(self, objects):
        assert await objects.list_prefix("trips/") == []

    async def test_list_prefix_returns_full_paths(self, objects):
        await objects.write("trips/a/trip.json", b"x")
        await objects.write("trips/b/trip.json", b"x")
        assert set(await objects.list_prefix("trips/")) == {
            "trips/a/trip.json",
            "trips/b/trip.json",
        }

    async def test_list_prefix_excludes_other_prefixes(self, objects):
        await objects.write("trips/a/trip.json", b"x")
        await objects.write("exports/a/route.gpx", b"x")
        assert await objects.list_prefix("trips/") == ["trips/a/trip.json"]

    async def test_list_prefix_pages_through_everything(self, objects):
        """Bucket listings are paginated; a store that reads page one silently loses trips."""
        for index in range(7):
            await objects.write(f"trips/trip-{index}/trip.json", b"x")
        assert len(await objects.list_prefix("trips/")) == 7

    async def test_paths_with_slashes_are_distinct_objects(self, objects):
        """`/` is part of the name, not a directory — encoding it wrong collapses trips."""
        await objects.write("trips/a/trip.json", b"a")
        await objects.write("trips/a-trip.json", b"b")
        assert await objects.read("trips/a/trip.json") == b"a"
        assert await objects.read("trips/a-trip.json") == b"b"
