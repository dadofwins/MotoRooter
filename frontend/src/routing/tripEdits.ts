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
import type { Coordinate, Poi, RouteLeg, TripLeg, Waypoint } from '../api/types'
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
  const span = target.end_waypoint_index - target.start_waypoint_index

  // Both out-of-range offsets produce a *contiguous* trip that saves cleanly, so nothing
  // downstream catches them. Offset 0 inserts at this leg's start index, which is the
  // previous leg's end: the user drags one leg and the one before it changes shape. Too
  // large and the via lands inside the following leg instead.
  if (input.offsetInLeg < 1 || input.offsetInLeg > span) {
    throw new RangeError(
      `offsetInLeg ${String(input.offsetInLeg)} is outside leg ${String(input.legIndex)}, ` +
        `which spans waypoints ${String(target.start_waypoint_index)}-${String(target.end_waypoint_index)}`,
    )
  }

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

/**
 * The leg whose drawn line passes closest to a point, if any.
 *
 * Segment-wise via `nearestPointOnPath`, so a leg that curves near the point beats one whose
 * endpoints happen to be closer. Unrouted legs have no line and cannot win.
 */
export function nearestLeg(
  legs: readonly TripLeg[],
  point: Coordinate,
): { legIndex: number; distanceM: number } | null {
  let best: { legIndex: number; distanceM: number } | null = null

  legs.forEach((leg, legIndex) => {
    const position = nearestPointOnPath(leg.routed?.geometry ?? [], point)
    if (position === null) return
    if (best === null || position.distanceM < best.distanceM) {
      best = { legIndex, distanceM: position.distanceM }
    }
  })

  return best
}

/**
 * Puts a discovered place on the route.
 *
 * The mouse path for "add to route", and the function the assistant's tool will call, so the
 * two cannot diverge in behaviour — the rule that chat is an accelerator rather than a
 * separate implementation.
 *
 * Inserted where it belongs *along* the route rather than appended: adding a campground
 * halfway through a trip should not send the rider back for it at the end.
 *
 * `null` when it cannot be done — an unverified suggestion, which the backend would reject,
 * or a trip with no routed leg to insert into.
 */
export function addPoiToRoute(edit: RouteEdit, poi: Poi): RouteEdit | null {
  // Mirrors the backend rule: an LLM suggestion that never resolved to a place_id is a
  // claim, not a place, and cannot be pinned to a route.
  if (poi.source === 'llm_suggested' && (poi.place_id ?? null) === null) return null

  const nearest = nearestLeg(edit.legs, poi.coordinate)
  if (nearest === null) return null
  const leg = edit.legs[nearest.legIndex]
  if (leg === undefined) return null

  const offsetInLeg = viaInsertionOffset({
    legWaypoints: legWaypoints(edit.waypoints, leg),
    geometry: leg.routed?.geometry ?? [],
    dragged: poi.coordinate,
  })

  const inserted = insertVia(edit, {
    legIndex: nearest.legIndex,
    offsetInLeg,
    coordinate: poi.coordinate,
  })

  // Named, so the route reads back as places rather than as coordinates.
  const at = leg.start_waypoint_index + offsetInLeg
  return {
    ...inserted,
    waypoints: inserted.waypoints.map((waypoint, index) =>
      index === at ? { ...waypoint, name: poi.name } : waypoint,
    ),
  }
}

/**
 * Half of the backend's rounding step, which is the largest a rounded value can differ from
 * the original.
 *
 * `COORDINATE_KEY_PRECISION` is 5 decimal places — about 1.1 m, chosen there to absorb float
 * jitter between two runs of the same drag while keeping genuinely different waypoints
 * distinct. Comparing exactly against a rounded fingerprint would report every leg stale.
 */
const FINGERPRINT_TOLERANCE_DEG = 0.5e-5

/**
 * Whether a leg's geometry still matches the waypoints it is supposed to connect.
 *
 * `insertVia` deliberately keeps the old geometry so the line does not blink out while the
 * new route is fetched, which leaves a leg whose geometry is briefly a lie. Inside a drag
 * that is safe because the commit overwrites it, but relying on call order makes every
 * other caller a latent bug.
 *
 * `RouteLeg.routed_from` records the request the geometry came from, so the question is
 * answerable from the data instead. Comparing the leg's *endpoints* to its waypoints could
 * not work: engines snap to the nearest routable node, sometimes by hundreds of metres, and
 * no tolerance separates that from a rider dragging a point.
 */
export function isLegStale(waypoints: readonly Waypoint[], leg: TripLeg): boolean {
  const fingerprint = leg.routed?.routed_from
  // No geometry is trivially stale. No fingerprint means it cannot be judged: reporting
  // fresh would hide a real mismatch, so it is reported stale, which merely costs a reroute.
  if (leg.routed === null || leg.routed === undefined || fingerprint === null || fingerprint === undefined) {
    return true
  }
  if (fingerprint.intent !== leg.intent) return true
  if ((fingerprint.provider_override ?? null) !== (leg.provider_override ?? null)) return true

  const current = legWaypoints(waypoints, leg)
  if (current.length !== fingerprint.waypoints.length) return true
  return current.some((point, index) => {
    const from = fingerprint.waypoints[index]
    if (from === undefined) return true
    return (
      Math.abs(point.lat - from.lat) > FINGERPRINT_TOLERANCE_DEG ||
      Math.abs(point.lon - from.lon) > FINGERPRINT_TOLERANCE_DEG
    )
  })
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
