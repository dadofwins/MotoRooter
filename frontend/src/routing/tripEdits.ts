/**
 * The trip edits a drag performs, as pure functions over waypoints and legs.
 *
 * Kept separate from the map and from React because this is where the drag interaction can
 * corrupt a trip. The backend validates that legs are **contiguous** — each starts where the
 * previous ended — and a drag inserts a waypoint mid-list, shifting every index after it.
 *
 * A via-point **extends** the affected leg rather than splitting it. That is what makes
 * "re-request the affected leg only" true: the leg gains an intermediate waypoint, one
 * provider call covers the change, and the neighbouring legs keep the geometry they already
 * have. Splitting instead would double the request count and re-route road the user never
 * touched.
 */
import type { Coordinate, RouteLeg, TripLeg, Waypoint } from '../api/types'
import { alongPath, nearestPointOnPath } from './geo'

/** The parts of a trip a drag changes. Narrower than `Trip` so callers can hold either. */
export interface RouteEdit {
  readonly waypoints: readonly Waypoint[]
  readonly legs: readonly TripLeg[]
}

export interface InsertViaInput {
  readonly legIndex: number
  /**
   * Position within the leg's own waypoint span: 1 inserts directly after the leg's start.
   * Use `viaInsertionOffset` to derive it from where the user grabbed the line.
   */
  readonly offsetInLeg: number
  readonly coordinate: Coordinate
}

function legAt(legs: readonly TripLeg[], legIndex: number): TripLeg {
  const leg = legs[legIndex]
  if (leg === undefined) {
    // Louder than a no-op on purpose: a drag that silently does nothing is
    // indistinguishable to the user from a routing failure.
    throw new RangeError(`no leg at index ${String(legIndex)}; the trip has ${String(legs.length)}`)
  }
  return leg
}

/** The coordinates to send as a leg's `waypoints`, in route order. */
export function legWaypoints(waypoints: readonly Waypoint[], leg: TripLeg): Coordinate[] {
  return waypoints
    .slice(leg.start_waypoint_index, leg.end_waypoint_index + 1)
    .map((waypoint) => waypoint.coordinate)
}

export interface ViaInsertionInput {
  /** The leg's waypoint coordinates, from `legWaypoints`. */
  readonly legWaypoints: readonly Coordinate[]
  /** The leg's current routed geometry. */
  readonly geometry: readonly Coordinate[]
  /** Where the user dragged to, or the point they grabbed. */
  readonly dragged: Coordinate
}

/**
 * Where within a leg's waypoint span a new via-point belongs.
 *
 * Ordered by position *along the route*, not by proximity to the existing waypoints. On a
 * route that doubles back — which a twisty-preferring route does constantly — the return
 * side passes close to the outbound side, and ordering by straight-line distance would drop
 * the via-point at the wrong end of the leg and reroute the wrong half of it.
 *
 * The result is always between 1 and `legWaypoints.length - 1`, so a via-point can never
 * displace the leg's own endpoints — those belong to the trip and to the neighbouring legs.
 * That bound is structural rather than clamped: only *intermediate* waypoints are counted,
 * of which there are `length - 2`, so the offset cannot run past the leg's end.
 */
export function viaInsertionOffset(input: ViaInsertionInput): number {
  const draggedPosition = nearestPointOnPath(input.geometry, input.dragged)
  if (draggedPosition === null) return 1

  const draggedAlong = alongPath(draggedPosition)
  let offset = 1
  for (const waypoint of input.legWaypoints.slice(1, -1)) {
    const position = nearestPointOnPath(input.geometry, waypoint)
    if (position === null || alongPath(position) > draggedAlong) break
    offset += 1
  }

  return offset
}

/**
 * Insert a user-placed via-point, preserving leg contiguity.
 *
 * The affected leg's geometry is deliberately left in place: the fast path renders the drag
 * optimistically and reconciles when the leg response lands, so discarding it here would
 * blank the route line for the duration of the request.
 */
export function insertVia(edit: RouteEdit, input: InsertViaInput): RouteEdit {
  const target = legAt(edit.legs, input.legIndex)
  const insertAt = target.start_waypoint_index + input.offsetInLeg

  const waypoints = [
    ...edit.waypoints.slice(0, insertAt),
    // Pinned: the user placed it by hand, so a replan must not move or drop it.
    { coordinate: input.coordinate, name: null, pinned: true },
    ...edit.waypoints.slice(insertAt),
  ]

  const legs = edit.legs.map((leg, index) => {
    if (index < input.legIndex) return leg // wholly before the insertion
    if (index === input.legIndex) return { ...leg, end_waypoint_index: leg.end_waypoint_index + 1 }
    return {
      ...leg,
      start_waypoint_index: leg.start_waypoint_index + 1,
      end_waypoint_index: leg.end_waypoint_index + 1,
    }
  })

  return { waypoints, legs }
}

/** Replace one leg's routed geometry, by identity leaving every other leg alone. */
export function spliceRoutedLeg(
  legs: readonly TripLeg[],
  legIndex: number,
  routed: RouteLeg | null,
): TripLeg[] {
  const target = legAt(legs, legIndex)
  return legs.map((leg, index) => (index === legIndex ? { ...target, routed } : leg))
}
