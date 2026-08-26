"""Trip edits, as functions rather than endpoints.

Extracted from the router because the assistant needs the same operations and must not
reimplement them. The rule from the root document — every tool is a thin wrapper over the
same service function the REST endpoint calls — only means anything if the function exists
to be shared; two implementations that agree today diverge by next week, and both keep
answering plausibly while they do.

Nothing here knows about HTTP. Errors are domain errors, and the API layer's exception
handlers turn them into status codes.
"""

from collections.abc import Sequence

from motorooter.routing.models import LegIntent, RouteLeg
from motorooter.trips.errors import TripModifiedConcurrently
from motorooter.trips.models import Poi, Trip, TripLeg, Waypoint, utc_now
from motorooter.trips.store import TripStore

MAX_UPDATE_ATTEMPTS = 2
"""Read-merge-write tries twice before reporting a conflict.

One retry resolves the ordinary case — two riders editing different fields of the same
shared trip — because re-merging a partial edit onto the newer document yields the union of
both. A writer that loses twice is contending with sustained traffic, and looping further
would spend requests without converging.
"""


MINIMUM_WAYPOINTS = 2
"""Below this there is no route to compute, so there are no legs either."""


def longest_routed_leg(trip: Trip) -> RouteLeg | None:
    """The leg worth measuring a trip against, or `None` if nothing has routed.

    The longest rather than the first: a trip is frequently one long ride and a short
    connector, and the connector is the wrong half of the map to search along and the wrong
    length to budget a detour against. Shared because it was written twice and the second
    copy — the assistant's — took the first leg instead, which on a trip that opens with a
    two-kilometre hop meant discovering places around the rider's driveway.
    """
    routed = trip.routed_legs
    return max(routed, key=lambda leg: leg.distance_m) if routed else None


def legs_for(trip: Trip, waypoints: Sequence[Waypoint]) -> tuple[TripLeg, ...]:
    """Legs spanning consecutive waypoint pairs, keeping each existing pair's intent.

    Rebuilt rather than patched because indices shift: a leg recorded as 2-3 means a
    different stretch of road once a waypoint is inserted ahead of it. Geometry is dropped —
    the leg is stale by definition — and the next route request fills it back in.
    """
    points = tuple(waypoints)
    if len(points) < MINIMUM_WAYPOINTS:
        return ()
    default = trip.intent_for_new_legs
    existing = {(leg.start_waypoint_index, leg.end_waypoint_index): leg for leg in trip.legs}
    return tuple(
        TripLeg(
            intent=existing[(index, index + 1)].intent
            if (index, index + 1) in existing
            else default,
            start_waypoint_index=index,
            end_waypoint_index=index + 1,
        )
        for index in range(len(points) - 1)
    )


def merged(
    existing: Trip,
    *,
    name: str | None = None,
    waypoints: Sequence[Waypoint] | None = None,
    legs: Sequence[TripLeg] | None = None,
    pois: Sequence[Poi] | None = None,
    default_intent: LegIntent | None = None,
) -> Trip:
    """Apply a partial edit to a trip.

    `edited_at` advances only when geometry actually changes, since it drives the replan
    staleness flag — bumping it on a rename would spuriously mark discovery stale.
    """
    geometry_changed = (waypoints is not None and tuple(waypoints) != existing.waypoints) or (
        legs is not None and tuple(legs) != existing.legs
    )

    updated = existing.model_copy(
        update={
            "name": name if name is not None else existing.name,
            "waypoints": tuple(waypoints) if waypoints is not None else existing.waypoints,
            "legs": tuple(legs) if legs is not None else existing.legs,
            "pois": tuple(pois) if pois is not None else existing.pois,
            "default_intent": (
                default_intent if default_intent is not None else existing.default_intent
            ),
            "edited_at": utc_now() if geometry_changed else existing.edited_at,
        }
    )
    # Revalidate: model_copy skips validators, and leg/waypoint consistency lives there.
    return Trip.model_validate(updated.model_dump())


async def edit_trip(
    store: TripStore,
    slug: str,
    *,
    name: str | None = None,
    waypoints: Sequence[Waypoint] | None = None,
    legs: Sequence[TripLeg] | None = None,
    pois: Sequence[Poi] | None = None,
    default_intent: LegIntent | None = None,
) -> Trip:
    """Apply a partial edit, refusing to clobber a concurrent one.

    Trips are public and world-editable, so two riders editing the same trip from a shared
    link is ordinary — and so is a rider dragging the route while the assistant edits it,
    which is the case that made this shared rather than duplicated. An unconditional write
    would not merely lose the slower writer's edit, it would roll back fields that writer
    never touched and answer as though the data had been saved.

    Raises:
        TripModifiedConcurrently: still contended after `MAX_UPDATE_ATTEMPTS`.
        TripNotFound: no such trip, including one deleted mid-edit — writing anyway would
            resurrect something somebody chose to remove.
    """
    for attempt in range(1, MAX_UPDATE_ATTEMPTS + 1):
        versioned = await store.get_versioned(slug)
        candidate = merged(
            versioned.trip,
            name=name,
            waypoints=waypoints,
            legs=legs,
            pois=pois,
            default_intent=default_intent,
        )
        try:
            return await store.put(candidate, if_version=versioned.version)
        except TripModifiedConcurrently:
            if attempt == MAX_UPDATE_ATTEMPTS:
                raise
    raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover
