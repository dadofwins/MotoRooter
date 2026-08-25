"""Shared contract every `ObjectStore` must satisfy.

Subclass `ObjectStoreContract` and override the `objects` fixture. The in-memory fake and
the Cloud Storage adapter both pass this suite, which is what makes it safe to run the
trip-store contract against the fake and trust the result.

The precondition tests are the load-bearing ones. `write(if_generation_match=...)` is what
lets a caller create without clobbering and replace without losing a concurrent edit; a fake
that gets those semantics even slightly wrong would hide the bug rather than expose it, and
the retry path built on top would then be dead code in development and live in production.
"""

import pytest

from motorooter.trips.objects import (
    MUST_NOT_EXIST,
    ObjectAlreadyExists,
    ObjectNotFound,
    ObjectStore,
    ObjectVersionMismatch,
)


class ObjectStoreContract:
    @pytest.fixture
    def objects(self) -> ObjectStore:
        raise NotImplementedError("override the `objects` fixture")

    def test_satisfies_the_protocol(self, objects):
        assert isinstance(objects, ObjectStore)

    async def test_write_then_read_returns_the_bytes(self, objects):
        await objects.write("trips/a/trip.json", b'{"slug":"a"}')
        assert (await objects.read("trips/a/trip.json")).data == b'{"slug":"a"}'

    async def test_read_missing_raises(self, objects):
        with pytest.raises(ObjectNotFound):
            await objects.read("trips/nope/trip.json")

    async def test_write_overwrites_by_default(self, objects):
        await objects.write("trips/a/trip.json", b"first")
        await objects.write("trips/a/trip.json", b"second")
        assert (await objects.read("trips/a/trip.json")).data == b"second"

    async def test_write_if_absent_creates(self, objects):
        await objects.write("trips/a/trip.json", b"first", if_generation_match=MUST_NOT_EXIST)
        assert (await objects.read("trips/a/trip.json")).data == b"first"

    async def test_write_if_absent_refuses_to_overwrite(self, objects):
        await objects.write("trips/a/trip.json", b"first")
        with pytest.raises(ObjectAlreadyExists):
            await objects.write("trips/a/trip.json", b"second", if_generation_match=MUST_NOT_EXIST)

    async def test_refused_write_leaves_the_original_intact(self, objects):
        """A rejected create must not be a partial write — readers see the old object."""
        await objects.write("trips/a/trip.json", b"first")
        with pytest.raises(ObjectAlreadyExists):
            await objects.write("trips/a/trip.json", b"second", if_generation_match=MUST_NOT_EXIST)
        assert (await objects.read("trips/a/trip.json")).data == b"first"

    async def test_read_reports_the_current_generation(self, objects):
        await objects.write("trips/a/trip.json", b"first")
        assert (await objects.read("trips/a/trip.json")).generation > 0

    async def test_write_returns_the_generation_it_created(self, objects):
        generation = await objects.write("trips/a/trip.json", b"first")
        assert (await objects.read("trips/a/trip.json")).generation == generation

    async def test_the_generation_changes_on_every_write(self, objects):
        """A version that does not move cannot detect a concurrent edit."""
        first = await objects.write("trips/a/trip.json", b"first")
        second = await objects.write("trips/a/trip.json", b"second")
        assert first != second

    async def test_generations_are_not_reused_across_objects(self, objects):
        """Sharing a counter is fine; handing two live objects the same version is not."""
        a = await objects.write("trips/a/trip.json", b"x")
        b = await objects.write("trips/b/trip.json", b"x")
        assert a != b

    async def test_conditional_write_succeeds_on_the_matching_generation(self, objects):
        generation = await objects.write("trips/a/trip.json", b"first")
        await objects.write("trips/a/trip.json", b"second", if_generation_match=generation)
        assert (await objects.read("trips/a/trip.json")).data == b"second"

    async def test_conditional_write_refuses_a_stale_generation(self, objects):
        """The read-merge-write case: someone else wrote between our read and our write."""
        stale = await objects.write("trips/a/trip.json", b"first")
        await objects.write("trips/a/trip.json", b"second")
        with pytest.raises(ObjectVersionMismatch):
            await objects.write("trips/a/trip.json", b"third", if_generation_match=stale)

    async def test_a_refused_conditional_write_does_not_touch_the_object(self, objects):
        """Losing the race must cost the loser's write, never the winner's."""
        stale = await objects.write("trips/a/trip.json", b"first")
        await objects.write("trips/a/trip.json", b"winner")
        with pytest.raises(ObjectVersionMismatch):
            await objects.write("trips/a/trip.json", b"loser", if_generation_match=stale)
        assert (await objects.read("trips/a/trip.json")).data == b"winner"

    async def test_conditional_write_to_a_missing_object_is_a_mismatch_not_a_create(self, objects):
        """It was deleted under us. Recreating it silently would resurrect deleted data."""
        with pytest.raises(ObjectVersionMismatch):
            await objects.write("trips/a/trip.json", b"x", if_generation_match=42)

    async def test_must_not_exist_is_distinguishable_from_a_stale_generation(self, objects):
        """Callers map these to different HTTP statuses: 409 conflict versus 409 modified."""
        await objects.write("trips/a/trip.json", b"first")
        with pytest.raises(ObjectAlreadyExists):
            await objects.write("trips/a/trip.json", b"x", if_generation_match=MUST_NOT_EXIST)

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
        assert (await objects.read("trips/a/trip.json")).data == b"a"
        assert (await objects.read("trips/a-trip.json")).data == b"b"
