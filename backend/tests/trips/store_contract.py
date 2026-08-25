"""Shared contract every `TripStore` must satisfy.

Subclass `TripStoreContract` and override the `store` fixture. The Cloud Storage
implementation must pass this suite unchanged — that is what makes it substitutable for
the in-memory one without the API noticing.

The round-trip fidelity tests matter most for a JSON-backed store: they are what catch
tuple-vs-list drift, dropped `None`s, and timezone loss on reload.
"""

from datetime import UTC, datetime, timedelta

import pytest

from motorooter.routing.models import Coordinate, LegIntent, RouteLeg, Surface, SurfaceSpan
from motorooter.trips.errors import (
    TripAlreadyExists,
    TripModifiedConcurrently,
    TripNotFound,
)
from motorooter.trips.models import Poi, PoiCategory, PoiSource, Trip, TripLeg, Waypoint

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def make_trip(slug: str = "oregon-backcountry", **overrides: object) -> Trip:
    defaults: dict[str, object] = {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "created_at": T0,
        "edited_at": T0,
        "waypoints": [
            Waypoint(coordinate=Coordinate(lat=45.0, lon=-121.0), name="Start"),
            Waypoint(coordinate=Coordinate(lat=46.0, lon=-121.0)),
        ],
        "legs": [TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1)],
    }
    return Trip(**(defaults | overrides))


def fully_populated_trip() -> Trip:
    """Every optional field set, so round-trip tests actually exercise them."""
    return make_trip(
        planned_at=T0 + timedelta(hours=1),
        legs=[
            TripLeg(
                intent=LegIntent.TECHNICAL_OFFROAD,
                start_waypoint_index=0,
                end_waypoint_index=1,
                provider_override="ors",
                routed=RouteLeg(
                    geometry=(
                        Coordinate(lat=45.0, lon=-121.0),
                        Coordinate(lat=45.5, lon=-121.0),
                        Coordinate(lat=46.0, lon=-121.0),
                    ),
                    distance_m=111_195.0,
                    duration_s=7200.0,
                    surface_spans=(
                        SurfaceSpan(start_index=0, end_index=1, surface=Surface.UNPAVED),
                        SurfaceSpan(start_index=1, end_index=2, surface=Surface.PAVED),
                    ),
                    ascent_m=1450.0,
                    provider="ors",
                    intent=LegIntent.TECHNICAL_OFFROAD,
                ),
            )
        ],
        pois=[
            Poi(
                id="poi-1",
                name="Ridge camp",
                category=PoiCategory.WILD_CAMP,
                coordinate=Coordinate(lat=45.4, lon=-121.1),
                source=PoiSource.USER,
                on_route=True,
                note="Water 1km north",
            ),
            Poi(
                id="poi-2",
                name="Diner",
                category=PoiCategory.FOOD,
                coordinate=Coordinate(lat=45.6, lon=-121.2),
                source=PoiSource.PLACES,
                place_id="ChIJ_example",
            ),
        ],
    )


class TripStoreContract:
    @pytest.fixture
    def store(self):
        raise NotImplementedError("override the `store` fixture")

    async def test_create_then_get_returns_the_trip(self, store):
        await store.create(make_trip())
        assert (await store.get("oregon-backcountry")).name == "Oregon Backcountry"

    async def test_get_missing_raises_trip_not_found(self, store):
        with pytest.raises(TripNotFound):
            await store.get("no-such-trip")

    async def test_create_duplicate_slug_raises(self, store):
        """Names are the primary key and everything is public; silent clobber is not ok."""
        await store.create(make_trip())
        with pytest.raises(TripAlreadyExists):
            await store.create(make_trip())

    async def test_put_overwrites_an_existing_trip(self, store):
        await store.create(make_trip())
        await store.put(make_trip(name="Renamed"))
        assert (await store.get("oregon-backcountry")).name == "Renamed"

    async def test_put_creates_when_absent(self, store):
        await store.put(make_trip())
        assert await store.exists("oregon-backcountry")

    async def test_exists_reports_absence(self, store):
        assert await store.exists("no-such-trip") is False

    async def test_delete_removes_the_trip(self, store):
        await store.create(make_trip())
        await store.delete("oregon-backcountry")
        assert await store.exists("oregon-backcountry") is False

    async def test_delete_missing_raises(self, store):
        with pytest.raises(TripNotFound):
            await store.delete("no-such-trip")

    async def test_list_is_empty_initially(self, store):
        assert await store.list() == []

    async def test_list_returns_a_summary_per_trip(self, store):
        await store.create(make_trip("trip-a"))
        await store.create(make_trip("trip-b"))
        assert {s.slug for s in await store.list()} == {"trip-a", "trip-b"}

    async def test_list_is_newest_edited_first(self, store):
        await store.create(make_trip("older", edited_at=T0))
        await store.create(make_trip("newer", edited_at=T0 + timedelta(hours=1)))
        assert [s.slug for s in await store.list()] == ["newer", "older"]

    async def test_list_does_not_ship_geometry(self, store):
        """The index must stay cheap; summaries carry totals, not routes."""
        await store.create(fully_populated_trip())
        summary = (await store.list())[0]
        assert not hasattr(summary, "legs")


class TripStoreVersioningContract:
    """Optimistic concurrency, required of every store.

    Held against the in-memory implementation as well as Cloud Storage on purpose. If only
    the durable store versioned, the API's retry-on-conflict path would be exercised in
    production and dead in every test — which is precisely how a concurrency bug survives a
    green suite.

    The failure this prevents is not merely a lost write. Two clients editing different
    fields of one trip each read version 1, each build "version 1 plus my change", and the
    second write rolls back the first one's field — while both are told they succeeded.
    """

    @pytest.fixture
    def store(self):
        raise NotImplementedError("override the `store` fixture")

    async def test_get_versioned_returns_the_trip_and_a_version(self, store):
        await store.create(make_trip())
        versioned = await store.get_versioned("oregon-backcountry")
        assert versioned.trip.name == "Oregon Backcountry"

    async def test_get_versioned_on_a_missing_trip_raises(self, store):
        with pytest.raises(TripNotFound):
            await store.get_versioned("no-such-trip")

    async def test_the_version_changes_when_the_trip_is_written(self, store):
        await store.create(make_trip())
        first = (await store.get_versioned("oregon-backcountry")).version
        await store.put(make_trip(name="Renamed"))
        assert (await store.get_versioned("oregon-backcountry")).version != first

    async def test_a_conditional_put_on_the_current_version_succeeds(self, store):
        await store.create(make_trip())
        versioned = await store.get_versioned("oregon-backcountry")
        await store.put(make_trip(name="Renamed"), if_version=versioned.version)
        assert (await store.get("oregon-backcountry")).name == "Renamed"

    async def test_a_conditional_put_on_a_stale_version_is_refused(self, store):
        await store.create(make_trip())
        stale = (await store.get_versioned("oregon-backcountry")).version
        await store.put(make_trip(name="Someone Else"))
        with pytest.raises(TripModifiedConcurrently):
            await store.put(make_trip(name="Mine"), if_version=stale)

    async def test_a_refused_put_leaves_the_winners_trip_intact(self, store):
        await store.create(make_trip())
        stale = (await store.get_versioned("oregon-backcountry")).version
        await store.put(make_trip(name="Winner"))
        with pytest.raises(TripModifiedConcurrently):
            await store.put(make_trip(name="Loser"), if_version=stale)
        assert (await store.get("oregon-backcountry")).name == "Winner"

    async def test_an_unconditional_put_still_overwrites(self, store):
        """The existing contract is unchanged; versioning is opt-in per call."""
        await store.create(make_trip())
        await store.put(make_trip(name="Renamed"))
        assert (await store.get("oregon-backcountry")).name == "Renamed"

    async def test_a_conditional_put_to_a_deleted_trip_does_not_resurrect_it(self, store):
        await store.create(make_trip())
        stale = (await store.get_versioned("oregon-backcountry")).version
        await store.delete("oregon-backcountry")
        with pytest.raises(TripModifiedConcurrently):
            await store.put(make_trip(name="Zombie"), if_version=stale)
        assert await store.exists("oregon-backcountry") is False

    async def test_read_merge_write_preserves_a_concurrent_edit_to_another_field(self, store):
        """The whole point. Sequential here; the conflict is what the retry loop needs."""
        await store.create(make_trip())
        mine = await store.get_versioned("oregon-backcountry")

        # Someone else renames the trip while we are editing waypoints.
        await store.put(make_trip(name="Their Name"))

        with pytest.raises(TripModifiedConcurrently):
            await store.put(
                mine.trip.model_copy(update={"waypoints": mine.trip.waypoints[:1], "legs": ()}),
                if_version=mine.version,
            )
        assert (await store.get("oregon-backcountry")).name == "Their Name"


class TripStoreRoundTripContract:
    """Fidelity checks. Separated so a serializing store can be pointed at these directly."""

    @pytest.fixture
    def store(self):
        raise NotImplementedError("override the `store` fixture")

    async def test_round_trips_a_fully_populated_trip(self, store):
        original = fully_populated_trip()
        await store.create(original)
        assert await store.get(original.slug) == original

    async def test_preserves_routed_geometry_exactly(self, store):
        original = fully_populated_trip()
        await store.create(original)
        reloaded = await store.get(original.slug)
        assert reloaded.legs[0].routed == original.legs[0].routed

    async def test_preserves_surface_spans(self, store):
        """Surface data drives the headline dirt statistic; losing it is silent."""
        original = fully_populated_trip()
        await store.create(original)
        reloaded = await store.get(original.slug)
        assert reloaded.total_unpaved_fraction == original.total_unpaved_fraction

    async def test_preserves_timezone_aware_timestamps(self, store):
        original = fully_populated_trip()
        await store.create(original)
        reloaded = await store.get(original.slug)
        assert reloaded.planned_at == original.planned_at
        assert reloaded.planned_at is not None
        assert reloaded.planned_at.tzinfo is not None

    async def test_preserves_optional_nulls(self, store):
        original = make_trip()
        await store.create(original)
        reloaded = await store.get(original.slug)
        assert reloaded.planned_at is None
        assert reloaded.legs[0].routed is None

    async def test_preserves_poi_fields(self, store):
        original = fully_populated_trip()
        await store.create(original)
        assert (await store.get(original.slug)).pois == original.pois

    async def test_stored_trip_is_isolated_from_later_mutation(self, store):
        """An in-memory store must not hand back a reference callers can edit underneath it."""
        original = fully_populated_trip()
        await store.create(original)
        first = await store.get(original.slug)
        second = await store.get(original.slug)
        assert first == second
