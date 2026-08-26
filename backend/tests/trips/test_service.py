"""Trip edits as functions, and which of them need a routing engine consulted.

`changed_legs` exists because validation was costing more than the edit. Every tool that
moves a waypoint routed the *whole* trip to check the points could be joined, so a
seven-point trip paid a seven-point request per waypoint added — and a failure anywhere in
that span refused an edit that had nothing to do with it. A live run caught exactly that:

    add_waypoint: those points could not be joined into a route: [google] ZERO_RESULTS

The waypoint being added was fine. Some other stretch of the trip was not.
"""

from datetime import UTC, datetime

from motorooter.routing.models import Coordinate, LegIntent
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
