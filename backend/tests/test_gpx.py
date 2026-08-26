"""GPX export: a track, ordered waypoints, and a point budget.

The device is the point. A line is navigable; a line with your campsite on it is a plan, so
discovered POIs travel as waypoints rather than being left behind in the browser.

**Decimated, never truncated.** CLAUDE.md is explicit and the reason is what truncation does
on a real route: a WABDR section is thousands of points, and cutting at the limit hands a
rider the first third of their day and silently drops the rest. Decimation keeps the whole
route and spends its budget on the corners, which is where the shape lives.
"""

import xml.etree.ElementTree as ElementTree

import pytest

from motorooter.gpx import (
    GARMIN_TRACK_POINT_LIMIT,
    GPX_NAMESPACE,
    decimate,
    trip_to_gpx,
)
from motorooter.routing.models import Coordinate, LegIntent, RouteLeg
from motorooter.trips.models import Poi, PoiCategory, PoiSource, Trip, TripLeg, Waypoint, utc_now

NS = {"gpx": GPX_NAMESPACE}


def line(count: int, *, spacing: float = 0.001) -> tuple[Coordinate, ...]:
    return tuple(Coordinate(lat=47.0 + i * spacing, lon=-121.0) for i in range(count))


def leg(geometry=None, *, intent: LegIntent = LegIntent.UNPAVED) -> RouteLeg:
    points = geometry if geometry is not None else line(10)
    return RouteLeg(
        geometry=points,
        distance_m=1000.0,
        duration_s=600.0,
        provider="fake",
        intent=intent,
    )


def poi(name: str, lat: float = 47.05, category: PoiCategory = PoiCategory.WILD_CAMP) -> Poi:
    return Poi(
        id=f"poi-{name}",
        name=name,
        category=category,
        coordinate=Coordinate(lat=lat, lon=-121.0),
        source=PoiSource.PLACES,
        place_id=f"ChIJ_{name}",
    )


def trip(*, legs=None, pois=(), waypoints=None, name: str = "Cascade Loop") -> Trip:
    now = utc_now()
    routed = legs if legs is not None else (leg(),)
    points = (
        waypoints
        if waypoints is not None
        else tuple(
            Waypoint(coordinate=Coordinate(lat=47.0 + i, lon=-121.0), name=f"Stop {i}")
            for i in range(len(routed) + 1)
        )
    )
    return Trip(
        slug="cascade-loop",
        name=name,
        created_at=now,
        edited_at=now,
        waypoints=points,
        legs=tuple(
            TripLeg(
                intent=LegIntent.UNPAVED,
                start_waypoint_index=index,
                end_waypoint_index=index + 1,
                routed=item,
            )
            for index, item in enumerate(routed)
        ),
        pois=pois,
    )


def parsed(document: str) -> ElementTree.Element:
    return ElementTree.fromstring(document)


def text_of(element: ElementTree.Element | None) -> str:
    """The element's text, insisting it is there.

    A helper rather than `# type: ignore` at each call: these assertions are about content,
    and a missing element should fail as a missing element rather than as an attribute error
    three lines later.
    """
    assert element is not None
    return element.text or ""


def number(value: str | None) -> float:
    assert value is not None
    return float(value)


class TestItIsValidGpx:
    def test_it_parses(self):
        assert parsed(trip_to_gpx(trip())) is not None

    def test_the_root_is_gpx(self):
        assert parsed(trip_to_gpx(trip())).tag == f"{{{GPX_NAMESPACE}}}gpx"

    def test_it_declares_a_creator(self):
        """Devices and desktop software both show it, and a file with no creator looks like
        it came from nowhere."""
        assert parsed(trip_to_gpx(trip())).get("creator")

    def test_it_declares_the_version(self):
        assert parsed(trip_to_gpx(trip())).get("version") == "1.1"

    def test_the_trip_name_is_in_the_metadata(self):
        root = parsed(trip_to_gpx(trip(name="WABDR Section 3")))
        assert text_of(root.find("gpx:metadata/gpx:name", NS)) == "WABDR Section 3"


class TestTheTrack:
    def test_the_geometry_becomes_track_points(self):
        root = parsed(trip_to_gpx(trip(legs=(leg(line(10)),))))
        assert len(root.findall(".//gpx:trkpt", NS)) == 10

    def test_legs_join_into_one_track(self):
        """One ride, one track. Two tracks is two things to select on the device."""
        root = parsed(trip_to_gpx(trip(legs=(leg(line(5)), leg(line(5))))))
        assert len(root.findall("gpx:trk", NS)) == 1

    def test_each_leg_is_its_own_segment(self):
        """Segments are how GPX says "these points are contiguous, that gap is not". Leg
        boundaries are exactly that, and joining them into one segment would draw a line
        across a gap the router never routed."""
        root = parsed(trip_to_gpx(trip(legs=(leg(line(5)), leg(line(5))))))
        assert len(root.findall(".//gpx:trkseg", NS)) == 2

    def test_coordinates_survive(self):
        root = parsed(trip_to_gpx(trip(legs=(leg(line(3)),))))
        first = root.findall(".//gpx:trkpt", NS)[0]
        assert number(first.get("lat")) == pytest.approx(47.0)
        assert number(first.get("lon")) == pytest.approx(-121.0)

    def test_an_unrouted_leg_contributes_nothing(self):
        """A trip mid-edit has legs with no geometry. That is not an error and not a gap in
        the file; there is simply nothing to draw yet."""
        document = trip_to_gpx(trip(legs=(leg(), None)))
        assert parsed(document).findall(".//gpx:trkseg", NS)


class TestTheWaypoints:
    def test_trip_waypoints_are_exported(self):
        root = parsed(trip_to_gpx(trip()))
        names = [text_of(element.find("gpx:name", NS)) for element in root.findall("gpx:wpt", NS)]
        assert "Stop 0" in names

    def test_discovered_pois_are_exported(self):
        """Most of the value on the device: a line is navigable, a line with your campsite on
        it is a plan."""
        root = parsed(trip_to_gpx(trip(pois=(poi("Halfway Flat"),))))
        names = [text_of(element.find("gpx:name", NS)) for element in root.findall("gpx:wpt", NS)]
        assert "Halfway Flat" in names

    def test_a_poi_carries_its_category(self):
        """`type` is what lets a device or desktop tool show a campsite differently from a
        fuel stop."""
        root = parsed(trip_to_gpx(trip(pois=(poi("Halfway Flat"),))))
        types = [
            element.findtext("gpx:type", namespaces=NS) for element in root.findall("gpx:wpt", NS)
        ]
        assert "wild_camp" in types

    def test_waypoints_keep_their_order(self):
        root = parsed(trip_to_gpx(trip()))
        names = [text_of(element.find("gpx:name", NS)) for element in root.findall("gpx:wpt", NS)]
        assert names[:2] == ["Stop 0", "Stop 1"]

    def test_an_unnamed_waypoint_still_gets_a_name(self):
        """A device list of blank rows is unusable, and GPX allows the omission."""
        points = (
            Waypoint(coordinate=Coordinate(lat=47.0, lon=-121.0)),
            Waypoint(coordinate=Coordinate(lat=48.0, lon=-121.0)),
        )
        root = parsed(trip_to_gpx(trip(waypoints=points)))
        assert all(
            element.findtext("gpx:name", namespaces=NS) for element in root.findall("gpx:wpt", NS)
        )


class TestTheGarminPointLimit:
    def test_a_long_route_is_brought_under_the_limit(self):
        root = parsed(trip_to_gpx(trip(legs=(leg(line(5000)),))))
        assert len(root.findall(".//gpx:trkpt", NS)) <= GARMIN_TRACK_POINT_LIMIT

    def test_a_short_route_is_untouched(self):
        root = parsed(trip_to_gpx(trip(legs=(leg(line(10)),))))
        assert len(root.findall(".//gpx:trkpt", NS)) == 10

    def test_the_whole_route_is_still_there(self):
        """The truncation failure: a rider gets the first third of their day and no warning.
        The last point of the decimated track must be the last point of the route."""
        geometry = line(5000)
        root = parsed(trip_to_gpx(trip(legs=(leg(geometry),))))
        last = root.findall(".//gpx:trkpt", NS)[-1]
        assert number(last.get("lat")) == pytest.approx(geometry[-1].lat)

    def test_the_budget_is_shared_across_legs(self):
        """Per-leg budgets would let a ten-leg trip ship ten times the limit."""
        legs = tuple(leg(line(2000)) for _ in range(5))
        root = parsed(trip_to_gpx(trip(legs=legs)))
        assert len(root.findall(".//gpx:trkpt", NS)) <= GARMIN_TRACK_POINT_LIMIT


class TestDecimation:
    def test_it_keeps_both_ends(self):
        points = line(1000)
        kept = decimate(points, limit=50)
        assert kept[0] == points[0]
        assert kept[-1] == points[-1]

    def test_it_respects_the_limit(self):
        assert len(decimate(line(1000), limit=50)) <= 50

    def test_it_leaves_a_short_line_alone(self):
        points = line(10)
        assert decimate(points, limit=50) == points

    def test_it_keeps_the_corner(self):
        """The reason this is Douglas-Peucker and not every-nth. A hairpin is the shape a
        rider cares about, and even sampling is as likely to drop it as any other point."""
        straight_out = [Coordinate(lat=47.0, lon=-121.0 + i * 0.001) for i in range(100)]
        corner = Coordinate(lat=47.05, lon=-120.9)
        straight_back = [Coordinate(lat=47.0 + 0.001 * i, lon=-120.8) for i in range(100)]
        points = (*straight_out, corner, *straight_back)
        kept = decimate(points, limit=20)
        assert corner in kept

    def test_two_points_survive_any_limit(self):
        points = line(2)
        assert decimate(points, limit=1) == points

    def test_it_handles_an_empty_line(self):
        assert decimate((), limit=10) == ()
