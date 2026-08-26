"""Trip and POI models — the vocabulary shared by the API, storage, and the frontend."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from motorooter.routing.models import Coordinate, LegIntent, RouteLeg, Surface, SurfaceSpan
from motorooter.trips.models import Poi, PoiCategory, PoiDetail, PoiSource, Trip, TripLeg, Waypoint

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def coord(lat: float = 45.0, lon: float = -121.0) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


METRES_PER_DEGREE_LAT = 111_195.0


def poi(**overrides: Any) -> Poi:
    fields: dict[str, Any] = {
        "id": "p1",
        "name": "Lion Rock Lookout",
        "category": PoiCategory.VIEWPOINT,
        "coordinate": coord(),
        "source": PoiSource.PLACES,
        "place_id": "ChIJ-lookout",
    }
    return Poi(**{**fields, **overrides})


def routed(distance_m: float = 100_000.0, unpaved: bool = False) -> RouteLeg:
    """A leg whose geometry actually measures `distance_m`.

    Generating honest geometry matters: surface ratios are computed from geometry, so a
    fixture whose geometry contradicts its reported distance tests nothing real.
    """
    delta = distance_m / METRES_PER_DEGREE_LAT
    geometry = (coord(45.0), coord(45.0 + delta / 2), coord(45.0 + delta))
    spans = (SurfaceSpan(start_index=0, end_index=2, surface=Surface.UNPAVED),) if unpaved else ()
    return RouteLeg(
        geometry=geometry,
        distance_m=distance_m,
        duration_s=3600.0,
        surface_spans=spans,
        provider="fake",
        intent=LegIntent.UNPAVED,
    )


def trip(**overrides: object) -> Trip:
    defaults: dict[str, object] = {
        "slug": "oregon-backcountry",
        "name": "Oregon Backcountry",
        "created_at": T0,
        "edited_at": T0,
        "waypoints": [Waypoint(coordinate=coord(45.0)), Waypoint(coordinate=coord(46.0))],
        "legs": [TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1)],
    }
    return Trip(**(defaults | overrides))


class TestPoi:
    def test_persists_only_the_place_id_from_places(self):
        """Google's terms allow indefinite storage of place_id and little else.

        Ratings, photos, and reviews must be re-fetched for display, so they must not
        exist as fields here — a field that exists will eventually get written.
        """
        forbidden = {"rating", "photos", "photo_urls", "reviews", "user_rating_count", "phone"}
        assert forbidden.isdisjoint(Poi.model_fields)

    def test_place_id_is_optional_for_unresolved_suggestions(self):
        poi = Poi(
            id="p1",
            name="Maybe a campsite",
            category=PoiCategory.WILD_CAMP,
            coordinate=coord(),
            source=PoiSource.LLM_SUGGESTED,
        )
        assert poi.place_id is None

    def test_llm_suggestion_without_a_place_id_is_unverified(self):
        poi = Poi(
            id="p1",
            name="Hallucinated diner",
            category=PoiCategory.FOOD,
            coordinate=coord(),
            source=PoiSource.LLM_SUGGESTED,
        )
        assert poi.is_verified is False

    def test_llm_suggestion_resolved_against_places_is_verified(self):
        poi = Poi(
            id="p1",
            name="Real diner",
            category=PoiCategory.FOOD,
            coordinate=coord(),
            source=PoiSource.LLM_SUGGESTED,
            place_id="ChIJ_real",
        )
        assert poi.is_verified is True

    def test_user_placed_poi_is_verified_without_a_place_id(self):
        """The user pointed at the map; there is nothing to second-guess."""
        poi = Poi(
            id="p1",
            name="My spot",
            category=PoiCategory.WILD_CAMP,
            coordinate=coord(),
            source=PoiSource.USER,
        )
        assert poi.is_verified is True

    def test_unverified_suggestion_cannot_be_added_to_the_route(self):
        """LLM output is candidates only; an unresolved one must never reach the route."""
        with pytest.raises(ValidationError, match="unverified"):
            Poi(
                id="p1",
                name="Hallucinated campsite",
                category=PoiCategory.WILD_CAMP,
                coordinate=coord(),
                source=PoiSource.LLM_SUGGESTED,
                on_route=True,
            )

    def test_verified_suggestion_can_be_added_to_the_route(self):
        poi = Poi(
            id="p1",
            name="Real campsite",
            category=PoiCategory.CAMPGROUND,
            coordinate=coord(),
            source=PoiSource.LLM_SUGGESTED,
            place_id="ChIJ_real",
            on_route=True,
        )
        assert poi.on_route is True


class TestPoiScore:
    """The judge's verdict, kept so a rider can act on it without paying for discovery again.

    Ours to store, unlike everything else discovery learns about a place: the score is a
    number we computed and the note is a sentence our model wrote. Neither is Places content,
    which is why they can live here when `rating` cannot.
    """

    def test_a_place_can_carry_the_score_that_judged_it(self):
        assert poi(score=0.85).score == 0.85

    def test_a_place_nobody_judged_has_no_score(self):
        """Absent, not zero. A user-dropped pin was never scored and is not therefore bad."""
        assert poi().score is None

    @pytest.mark.parametrize("bad", [-0.1, 1.1])
    def test_a_score_outside_the_scale_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            poi(score=bad)

    def test_the_note_holds_the_reason_in_the_judge_s_words(self):
        reason = "Close, well-regarded lookout with unpaved approaches."
        assert poi(note=reason).note == reason


class TestPoiDetail:
    def test_carries_the_display_fields_that_must_not_be_persisted(self):
        detail = PoiDetail(
            poi=Poi(
                id="p1",
                name="Diner",
                category=PoiCategory.FOOD,
                coordinate=coord(),
                source=PoiSource.PLACES,
                place_id="ChIJ_real",
            ),
            rating=4.5,
            user_rating_count=210,
            photo_urls=["https://example.test/photo.jpg"],
        )
        assert detail.rating == 4.5

    def test_is_a_separate_type_from_the_persisted_poi(self):
        """Keeping them distinct is what stops display data leaking into storage."""
        assert "rating" not in Poi.model_fields
        assert "rating" in PoiDetail.model_fields


class TestWaypointsAndLegs:
    def test_leg_indices_must_reference_real_waypoints(self):
        with pytest.raises(ValidationError, match="waypoint"):
            trip(
                legs=[
                    TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=9)
                ]
            )

    def test_leg_must_move_forward(self):
        with pytest.raises(ValidationError):
            TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=2, end_waypoint_index=1)

    def test_legs_must_be_contiguous(self):
        """A gap between legs means the exported route silently teleports."""
        waypoints = [Waypoint(coordinate=coord(45.0 + i)) for i in range(4)]
        with pytest.raises(ValidationError, match="contiguous"):
            trip(
                waypoints=waypoints,
                legs=[
                    TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1),
                    TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=2, end_waypoint_index=3),
                ],
            )

    def test_contiguous_legs_are_accepted(self):
        waypoints = [Waypoint(coordinate=coord(45.0 + i)) for i in range(3)]
        assert trip(
            waypoints=waypoints,
            legs=[
                TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=1),
                TripLeg(
                    intent=LegIntent.HIGHWAY_CONNECTOR,
                    start_waypoint_index=1,
                    end_waypoint_index=2,
                ),
            ],
        )

    def test_a_trip_may_start_with_no_waypoints(self):
        """The greeting state: nothing set yet, and that is a valid trip to save."""
        assert trip(waypoints=[], legs=[]).waypoints == ()

    def test_each_leg_can_pin_its_own_provider(self):
        """Per-section engine choice is trip data, not a routing-code branch."""
        leg = TripLeg(
            intent=LegIntent.UNPAVED,
            start_waypoint_index=0,
            end_waypoint_index=1,
            provider_override="google",
        )
        assert leg.provider_override == "google"


class TestReplanState:
    def test_a_never_planned_trip_needs_a_replan(self):
        assert trip().needs_replan is True

    def test_planning_after_the_last_edit_clears_the_flag(self):
        assert trip(planned_at=T0 + timedelta(minutes=1)).needs_replan is False

    def test_editing_after_the_last_plan_sets_the_flag(self):
        """The stale-suggestions guard: POIs were found for an older route."""
        assert trip(planned_at=T0, edited_at=T0 + timedelta(minutes=1)).needs_replan is True

    def test_flag_is_derived_not_stored(self):
        """A stored boolean would drift out of sync with the timestamps."""
        assert "needs_replan" not in Trip.model_fields


class TestTotals:
    def test_distance_sums_routed_legs(self):
        t = trip(
            legs=[
                TripLeg(
                    intent=LegIntent.UNPAVED,
                    start_waypoint_index=0,
                    end_waypoint_index=1,
                    routed=routed(distance_m=50_000.0),
                )
            ]
        )
        assert t.total_distance_m == 50_000.0

    def test_unrouted_legs_contribute_nothing(self):
        assert trip().total_distance_m == 0.0

    def test_unpaved_fraction_is_weighted_by_leg_distance(self):
        """A short dirt leg beside a long paved one must not read as half the trip."""
        waypoints = [Waypoint(coordinate=coord(45.0 + i)) for i in range(3)]
        t = trip(
            waypoints=waypoints,
            legs=[
                TripLeg(
                    intent=LegIntent.UNPAVED,
                    start_waypoint_index=0,
                    end_waypoint_index=1,
                    routed=routed(distance_m=100_000.0, unpaved=True),
                ),
                TripLeg(
                    intent=LegIntent.HIGHWAY_CONNECTOR,
                    start_waypoint_index=1,
                    end_waypoint_index=2,
                    routed=routed(distance_m=300_000.0, unpaved=False),
                ),
            ],
        )
        assert t.total_unpaved_fraction == pytest.approx(0.25, rel=0.01)

    def test_unpaved_fraction_of_an_unrouted_trip_is_zero(self):
        assert trip().total_unpaved_fraction == 0.0


class TestDefaultIntent:
    """What kind of trip this is, remembered on the trip rather than only on its legs.

    Without it the mode lives *only* in legs that currently exist, so a trip stripped back
    to one waypoint and rebuilt comes back paved — silently, and after the rider said
    "as much fun offroad as possible".
    """

    def test_a_trip_need_not_say_what_kind_it_is(self):
        """Absent, not paved. Most trips are built by mouse and never state a preference."""
        assert trip().default_intent is None

    def test_a_trip_can_say_what_kind_it_is(self):
        assert trip(default_intent=LegIntent.UNPAVED).default_intent is LegIntent.UNPAVED


class TestSchemaEvolution:
    def test_carries_a_schema_version(self):
        """Trips are JSON in a bucket; migrations need a version to branch on."""
        assert trip().schema_version == 1


class TestTimestamps:
    def test_rejects_naive_datetimes(self):
        """A naive timestamp compared against an aware one raises at runtime."""
        with pytest.raises(ValidationError):
            trip(created_at=datetime(2026, 8, 25, 12, 0))
