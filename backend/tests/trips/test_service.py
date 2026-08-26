"""Trip edits as functions, and which of them need a routing engine consulted.

`changed_legs` exists because validation was costing more than the edit. Every tool that
moves a waypoint routed the *whole* trip to check the points could be joined, so a
seven-point trip paid a seven-point request per waypoint added — and a failure anywhere in
that span refused an edit that had nothing to do with it. A live run caught exactly that:

    add_waypoint: those points could not be joined into a route: [google] ZERO_RESULTS

The waypoint being added was fine. Some other stretch of the trip was not.
"""

from datetime import UTC, datetime

from motorooter.error_codes import ErrorCode
from motorooter.routing.models import (
    Coordinate,
    LegIntent,
    RouteFingerprint,
    RouteLeg,
    RouteRequest,
)
from motorooter.trips.models import DEFAULT_INTENT, Trip, TripLeg, Waypoint
from motorooter.trips.service import changed_legs, legs_for

T0 = datetime(2026, 8, 26, tzinfo=UTC)


def at(index: int) -> Waypoint:
    return Waypoint(coordinate=Coordinate(lat=47.0 + index / 10, lon=-121.0), name=f"w{index}")


def trip(count: int = 3, *, intent: LegIntent = LegIntent.UNPAVED) -> Trip:
    points = tuple(at(index) for index in range(count))
    return Trip(
        slug="t",
        name="T",
        created_at=T0,
        edited_at=T0,
        waypoints=points,
        legs=tuple(
            TripLeg(intent=intent, start_waypoint_index=index, end_waypoint_index=index + 1)
            for index in range(count - 1)
        ),
    )


def spans(legs, waypoints):
    return [
        (waypoints[leg.start_waypoint_index].name, waypoints[leg.end_waypoint_index].name)
        for leg in legs
    ]


class TestGeometryThatSurvivesARebuild:
    """Rebuilding legs threw away every leg's geometry, including legs nothing had touched.

    That is why a chat-built trip arrived with `total_distance_m` of 0 and why the
    route-through button refuses a second press until a browser has re-routed. The geometry
    was paid for and is still correct; only the legs the edit actually changed are stale.
    """

    @staticmethod
    def _routed(trip_, index):
        """Geometry stamped as coming from exactly the request leg `index` describes."""
        leg = trip_.legs[index]
        span = trip_.waypoints[leg.start_waypoint_index : leg.end_waypoint_index + 1]
        request = RouteRequest(
            waypoints=tuple(point.coordinate for point in span), intent=leg.intent
        )
        return RouteLeg(
            geometry=tuple(point.coordinate for point in span),
            distance_m=1000.0,
            duration_s=60.0,
            provider="fake",
            intent=leg.intent,
            routed_from=RouteFingerprint.of(request),
        )

    def _fully_routed(self, count: int = 4) -> Trip:
        base = trip(count)
        return base.model_copy(
            update={
                "legs": tuple(
                    leg.model_copy(update={"routed": self._routed(base, index)})
                    for index, leg in enumerate(base.legs)
                )
            }
        )

    def test_a_stretch_nobody_touched_keeps_the_geometry_it_had(self):
        before = self._fully_routed()
        after = (*before.waypoints, at(9))
        rebuilt = legs_for(before, after)
        assert [leg.routed is not None for leg in rebuilt] == [True, True, True, False]

    def test_the_stretch_an_insertion_replaced_loses_its_geometry(self):
        before = self._fully_routed()
        after = (before.waypoints[0], at(9), *before.waypoints[1:])
        rebuilt = legs_for(before, after)
        assert [leg.routed is not None for leg in rebuilt] == [False, False, True, True]

    def test_geometry_survives_the_indices_shifting_under_it(self):
        """The reason it cannot be keyed on index: an insertion renumbers everything after."""
        before = self._fully_routed()
        after = (before.waypoints[0], at(9), *before.waypoints[1:])
        rebuilt = legs_for(before, after)
        assert rebuilt[3].start_waypoint_index == 3
        assert rebuilt[3].routed is not None

    def test_a_stretch_whose_mode_changed_loses_its_geometry(self):
        """Dirt geometry under a Fast label renders perfectly and is a lie."""
        before = self._fully_routed()
        repointed = before.model_copy(
            update={"default_intent": LegIntent.HIGHWAY_CONNECTOR, "legs": ()}
        )
        rebuilt = legs_for(repointed, before.waypoints)
        assert all(leg.routed is None for leg in rebuilt)

    def test_geometry_that_was_already_stale_is_not_resurrected(self):
        before = self._fully_routed()
        moved = (*before.waypoints[:2], at(9), *before.waypoints[3:])
        rebuilt = legs_for(before, moved)
        assert [leg.routed is not None for leg in rebuilt] == [True, False, False]

    def test_geometry_that_no_longer_matches_its_own_stretch_is_dropped(self):
        """The pair of ends is unchanged and the geometry is still wrong.

        A rider can drag a waypoint through `PUT /api/trips/{slug}` and save without routing,
        which leaves a leg whose stored geometry came from a request it no longer describes.
        A later rebuild sees the same two ends and must not take that as permission to keep
        it — the ends are not what makes geometry current, the request is.
        """
        before = self._fully_routed(3)
        first = before.legs[0].routed
        assert first is not None
        drifted = before.model_copy(
            update={
                "legs": (
                    before.legs[0].model_copy(
                        update={
                            "routed": first.model_copy(
                                update={
                                    "routed_from": RouteFingerprint.of(
                                        RouteRequest(
                                            waypoints=(at(0).coordinate, at(7).coordinate),
                                            intent=before.legs[0].intent,
                                        )
                                    )
                                }
                            )
                        }
                    ),
                    *before.legs[1:],
                )
            }
        )
        rebuilt = legs_for(drifted, (*drifted.waypoints, at(9)))
        assert rebuilt[0].routed is None
        assert rebuilt[1].routed is not None

    def test_a_leg_spanning_three_points_loses_its_geometry_when_the_middle_goes(self):
        """Its two ends survive the removal; the road between them is a different road."""
        points = (at(0), at(1), at(2))
        through = Trip(
            slug="t",
            name="T",
            created_at=T0,
            edited_at=T0,
            waypoints=points,
            legs=(TripLeg(intent=LegIntent.UNPAVED, start_waypoint_index=0, end_waypoint_index=2),),
        )
        request = RouteRequest(
            waypoints=tuple(point.coordinate for point in points), intent=LegIntent.UNPAVED
        )
        routed_through = through.model_copy(
            update={
                "legs": (
                    through.legs[0].model_copy(
                        update={
                            "routed": RouteLeg(
                                geometry=tuple(point.coordinate for point in points),
                                distance_m=2000.0,
                                duration_s=120.0,
                                provider="fake",
                                intent=LegIntent.UNPAVED,
                                routed_from=RouteFingerprint.of(request),
                            )
                        }
                    ),
                )
            }
        )
        rebuilt = legs_for(routed_through, (at(0), at(2)))
        assert rebuilt[0].routed is None

    def test_a_leg_that_failed_to_route_stays_unrouted_rather_than_being_retried(self):
        """Its neighbour changing is not a reason to spend a request on it again."""
        before = self._fully_routed()
        broken = before.model_copy(
            update={
                "legs": (
                    before.legs[0].model_copy(
                        update={"routed": None, "last_routing_error": ErrorCode.NO_ROUTE_FOUND}
                    ),
                    *before.legs[1:],
                )
            }
        )
        after = (*broken.waypoints, at(9))
        assert changed_legs(broken, after) == legs_for(broken, after)[-1:]


class TestWhatNeedsRouting:
    def test_appending_a_point_only_asks_about_the_new_last_stretch(self):
        before = trip(3)
        after = (*before.waypoints, at(9))
        assert spans(changed_legs(before, after), after) == [("w2", "w9")]

    def test_inserting_a_point_asks_about_the_two_stretches_it_creates(self):
        before = trip(3)
        after = (before.waypoints[0], at(9), *before.waypoints[1:])
        assert spans(changed_legs(before, after), after) == [("w0", "w9"), ("w9", "w1")]

    def test_removing_a_point_asks_about_the_stretch_that_closes_the_gap(self):
        before = trip(4)
        after = (before.waypoints[0], *before.waypoints[2:])
        assert spans(changed_legs(before, after), after) == [("w0", "w2")]

    def test_the_stretches_nobody_touched_are_not_asked_about_again(self):
        """The whole point: a seven-point trip must not pay a seven-point request per edit."""
        before = trip(7)
        after = (*before.waypoints, at(9))
        assert len(changed_legs(before, after)) == 1

    def test_reordering_asks_about_every_stretch_that_is_new(self):
        before = trip(3)
        after = (before.waypoints[0], before.waypoints[2], before.waypoints[1])
        assert spans(changed_legs(before, after), after) == [("w0", "w2"), ("w2", "w1")]

    def test_an_unchanged_list_needs_nothing_routed(self):
        before = trip(4)
        assert changed_legs(before, before.waypoints) == ()

    def test_a_first_pair_on_an_empty_trip_is_new(self):
        before = trip(0)
        after = (at(0), at(1))
        assert spans(changed_legs(before, after), after) == [("w0", "w1")]

    def test_a_single_waypoint_has_no_stretch_to_route(self):
        assert changed_legs(trip(0), (at(0),)) == ()


class TestTheIntentItWouldRouteWith:
    def test_a_new_stretch_takes_the_mode_the_trip_says(self):
        before = trip(3, intent=LegIntent.UNPAVED)
        stated = before.model_copy(update={"default_intent": LegIntent.HIGHWAY_CONNECTOR})
        after = (*stated.waypoints, at(9))
        assert changed_legs(stated, after)[0].intent is LegIntent.HIGHWAY_CONNECTOR

    def test_with_nothing_stated_it_is_the_product_default(self):
        before = trip(3, intent=LegIntent.TWISTY_PAVED)
        after = (*before.waypoints, at(9))
        assert changed_legs(before, after)[0].intent is DEFAULT_INTENT

    def test_it_agrees_with_the_legs_that_will_be_saved(self):
        """Routing a stretch as one mode and saving it as another is worse than either."""
        before = trip(3)
        after = (before.waypoints[0], at(9), *before.waypoints[1:])
        saved = legs_for(before, after)
        for leg in changed_legs(before, after):
            match = next(
                candidate
                for candidate in saved
                if candidate.start_waypoint_index == leg.start_waypoint_index
            )
            assert match.intent is leg.intent
