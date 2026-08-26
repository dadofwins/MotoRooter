/**
 * The shape of a trip: which waypoints each leg spans, and which legs an edit invalidates.
 *
 * A trip used to be a single leg spanning every waypoint, and that one shortcut caused three
 * separate problems: per-segment routing modes were impossible because there was only ever one
 * intent; a drag re-routed the whole trip because "the affected leg" was all of it; and a trip
 * that is highway, then dirt, then highway could not be expressed at all.
 *
 * So legs partition the waypoints — leg N ends on the waypoint leg N+1 starts from, which is
 * the contiguity rule the backend enforces on save. Every function here preserves it.
 *
 * **The invariant that matters is the other one: an edit must not invalidate a leg it did not
 * touch.** Untouched legs are returned by identity, complete with their geometry, so nothing
 * re-requests road the rider never changed. That is not an optimisation — every provider call
 * is metered and the ORS free tier is this app's binding constraint.
 *
 * Legs are *spans*, not pairs. A leg starts life covering two waypoints and grows when a drag
 * inserts a via-point inside it (`insertVia`), which is why nothing here assumes
 * `end === start + 1`.
 */
import type { LegIntent, TripLeg, Waypoint } from '../api/types'
import type { RouteEdit } from './tripEdits'

/**
 * Intents that route through an engine able to report surface.
 *
 * Not a stylistic preference — a correctness constraint. An intent that resolves to an engine
 * with no surface data returns zero spans, every metre renders as `unknown` grey, and the one
 * distinction this app exists to draw silently disappears. Measured on Woodinville → Cashmere
 * → Ellensburg: `twisty_paved` resolves to Google and reports 0 spans over 270 km; `unpaved`
 * resolves to ORS and reports 139.
 *
 * Kept as *intents* rather than provider names deliberately: which engine serves an intent is
 * the backend's policy table to decide, and naming one here would hardcode today's answer to a
 * question that is configuration.
 */
export const SURFACE_REPORTING_INTENTS = ['unpaved', 'technical_offroad'] as const

/**
 * The intent a new leg gets until the rider chooses one.
 *
 * Dirt is the point of an adventure motorcycle planner, and — see above — it is also the only
 * way the rider sees any surface information at all. Per-leg choice is what the picker branch
 * adds; this is the seed, and it is the same value `Trip.default_intent` will carry once the
 * backend has it.
 */
export const DEFAULT_INTENT: LegIntent = 'unpaved'

/** A leg with no geometry yet: a span the router has not been asked about. */
function unrouted(intent: LegIntent, start: number, end: number): TripLeg {
  return {
    intent,
    start_waypoint_index: start,
    end_waypoint_index: end,
    provider_override: null,
    routed: null,
    last_routing_error: null,
  }
}

/**
 * The default structure for a list of waypoints: one leg per consecutive pair.
 *
 * The pairwise split is what makes per-segment modes possible at all — a rider cannot choose
 * dirt for the middle of a trip that has no middle. Fewer than two waypoints is not a trip
 * with no legs, it is a trip with nowhere to go, and produces none.
 */
export function legsSpanning(waypointCount: number, intent: LegIntent): TripLeg[] {
  const legs: TripLeg[] = []
  for (let start = 0; start + 1 < waypointCount; start++) {
    legs.push(unrouted(intent, start, start + 1))
  }
  return legs
}

/**
 * Adds a waypoint to the end of the trip, and one leg to reach it.
 *
 * Every existing leg is returned by identity. This is the case that used to be worst: adding a
 * fourth point discarded the geometry of the first three, so building a trip click by click
 * re-routed the whole thing on every click, and both the latency and the bill grew with the
 * length of the trip.
 *
 * The new leg starts from the last leg's *end*, not from one before the end of the waypoint
 * list — a leg extended by dragging spans three or more waypoints, and counting backwards from
 * the list would start the new leg inside it.
 */
export function withWaypointAppended(
  edit: RouteEdit,
  waypoint: Waypoint,
  intent: LegIntent,
): RouteEdit {
  const waypoints = [...edit.waypoints, waypoint]
  const end = waypoints.length - 1
  if (end < 1) return { waypoints, legs: [] }

  const start = edit.legs.at(-1)?.end_waypoint_index ?? 0
  return { waypoints, legs: [...edit.legs, unrouted(intent, start, end)] }
}

/**
 * Removes a waypoint, and re-shapes only the legs that touched it.
 *
 * Three cases, and they are genuinely different:
 *
 * - **A waypoint interior to one leg** — a via-point from a drag. That leg shrinks and loses
 *   its geometry; no boundary moves, so no neighbour is involved.
 * - **A boundary between two legs.** They become one leg, which has never been routed between
 *   the places it now connects. It takes the *first* half's intent: that is the segment the
 *   rider was working in, and defaulting instead would silently retarmac someone's dirt.
 * - **The trip's own start or end.** The neighbouring waypoint becomes the new terminus. If
 *   the outer leg spanned only two waypoints it disappears; if a drag had extended it, it
 *   survives one waypoint shorter.
 *
 * Everything else shifts index and keeps its geometry.
 */
export function withWaypointRemoved(edit: RouteEdit, index: number): RouteEdit {
  if (!Number.isInteger(index) || index < 0 || index >= edit.waypoints.length) {
    // Louder than a no-op. Silently ignoring it leaves legs pointing at waypoints that no
    // longer exist, which the backend rejects on save — long after the cause is unfindable.
    throw new RangeError(
      `no waypoint at index ${String(index)}; the trip has ${String(edit.waypoints.length)}`,
    )
  }

  const waypoints = [...edit.waypoints.slice(0, index), ...edit.waypoints.slice(index + 1)]
  // Nowhere left to go, or no structure to preserve: the caller builds one from scratch.
  if (waypoints.length < 2 || edit.legs.length === 0) return { waypoints, legs: [] }

  /** Old index to new. */
  const shift = (at: number): number => (at > index ? at - 1 : at)
  /** New index back to old. Never returns `index` itself — that waypoint is gone. */
  const unshift = (at: number): number => (at >= index ? at + 1 : at)

  // Legs are defined by their boundaries, so the edit is easiest to express as one: drop the
  // removed waypoint if it was a boundary, then re-anchor the ends.
  const first = edit.legs[0]?.start_waypoint_index ?? 0
  const boundaries = [first, ...edit.legs.map((leg) => leg.end_waypoint_index)]
    .filter((at) => at !== index)
    .map(shift)

  // Removing a terminus promotes its neighbour, which may have been an interior waypoint and
  // so absent from the boundary list. Both guards are no-ops when the outer leg spanned two
  // waypoints, because the surviving boundary already shifted onto the new terminus.
  if (boundaries[0] !== 0) boundaries.unshift(0)
  const last = waypoints.length - 1
  if (boundaries.at(-1) !== last) boundaries.push(last)

  /** The old leg covering exactly this span, if the removal left it untouched. */
  const carried = (start: number, end: number): TripLeg | undefined => {
    const before = edit.legs.find(
      (leg) => leg.start_waypoint_index === start && leg.end_waypoint_index === end,
    )
    if (before === undefined) return undefined
    // The removed waypoint was inside it, so its geometry describes a route through a place
    // that is no longer on the trip.
    if (start < index && index < end) return undefined
    return before
  }

  const legs: TripLeg[] = []
  for (let at = 0; at + 1 < boundaries.length; at++) {
    const start = boundaries[at] ?? 0
    const end = boundaries[at + 1] ?? 0
    const before = carried(unshift(start), unshift(end))
    if (before === undefined) {
      // Merged, shrunk, or newly spanning: the intent comes from whichever leg used to start
      // here, so a mode the rider chose survives a removal next to it.
      const inherited = edit.legs.find((leg) => leg.start_waypoint_index === unshift(start))
      legs.push(unrouted(inherited?.intent ?? edit.legs[0]?.intent ?? 'unpaved', start, end))
      continue
    }
    // Identity where nothing moved at all, so a caller comparing by reference sees no change.
    legs.push(
      before.start_waypoint_index === start && before.end_waypoint_index === end
        ? before
        : { ...before, start_waypoint_index: start, end_waypoint_index: end },
    )
  }

  return { waypoints, legs }
}
