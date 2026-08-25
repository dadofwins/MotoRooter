import { describe, expect, it } from 'vitest'
import { insertVia, legWaypoints, spliceRoutedLeg, viaInsertionOffset } from './tripEdits'
import type { Coordinate, RouteLeg, TripLeg, Waypoint } from '../api/types'

/**
 * The trip edits a drag performs.
 *
 * The backend validates that legs are **contiguous** — each leg starts where the previous one
 * ended — and rejects a trip where they are not. A drag inserts a waypoint into the middle of
 * the list, which shifts every index after it, so contiguity is exactly what these edits can
 * break. It is asserted directly rather than assumed.
 *
 * A via-point *extends* the affected leg rather than splitting it. That is what makes "re-request
 * the affected leg only" true: the leg gains an intermediate waypoint, its neighbours keep their
 * geometry, and one provider call covers the change.
 */

function waypoint(lat: number, lon = -120): Waypoint {
  return { coordinate: { lat, lon }, name: null, pinned: true }
}

function routed(geometry: readonly Coordinate[]): RouteLeg {
  return {
    geometry: [...geometry],
    distance_m: 1000,
    duration_s: 60,
    provider: 'fake',
    intent: 'unpaved',
    surface_spans: [],
    ascent_m: null,
  }
}

function leg(start: number, end: number, geometry: readonly Coordinate[] = []): TripLeg {
  return {
    intent: 'unpaved',
    start_waypoint_index: start,
    end_waypoint_index: end,
    provider_override: null,
    routed: geometry.length === 0 ? null : routed(geometry),
  }
}

/** The invariant the backend enforces. Any edit that breaks this is rejected on save. */
function assertContiguous(legs: readonly TripLeg[], waypointCount: number): void {
  legs.forEach((current, index) => {
    const previous = legs[index - 1]
    if (previous !== undefined) {
      expect(current.start_waypoint_index).toBe(previous.end_waypoint_index)
    }
    expect(current.end_waypoint_index).toBeLessThanOrEqual(waypointCount - 1)
    expect(current.start_waypoint_index).toBeGreaterThanOrEqual(0)
  })
}

describe('legWaypoints', () => {
  it('collects the coordinates a leg spans, in order', () => {
    const waypoints = [waypoint(47), waypoint(48), waypoint(49), waypoint(50)]

    expect(legWaypoints(waypoints, leg(1, 3))).toEqual([
      { lat: 48, lon: -120 },
      { lat: 49, lon: -120 },
      { lat: 50, lon: -120 },
    ])
  })

  it('is the request payload for a two-waypoint leg', () => {
    const waypoints = [waypoint(47), waypoint(48)]

    expect(legWaypoints(waypoints, leg(0, 1))).toHaveLength(2)
  })
})

describe('viaInsertionOffset', () => {
  const straight: Coordinate[] = [
    { lat: 47.0, lon: -120 },
    { lat: 47.5, lon: -120 },
    { lat: 48.0, lon: -120 },
    { lat: 48.5, lon: -120 },
    { lat: 49.0, lon: -120 },
  ]

  it('can only be 1 on a leg with no intermediate waypoints', () => {
    const offset = viaInsertionOffset({
      legWaypoints: [
        { lat: 47, lon: -120 },
        { lat: 49, lon: -120 },
      ],
      geometry: straight,
      dragged: { lat: 48, lon: -119.99 },
    })

    expect(offset).toBe(1)
  })

  it('lands before an existing via-point when the drag is upstream of it', () => {
    const offset = viaInsertionOffset({
      legWaypoints: [
        { lat: 47, lon: -120 },
        { lat: 48.5, lon: -120 }, // existing via
        { lat: 49, lon: -120 },
      ],
      geometry: straight,
      dragged: { lat: 47.5, lon: -119.99 },
    })

    expect(offset).toBe(1)
  })

  it('lands after an existing via-point when the drag is downstream of it', () => {
    const offset = viaInsertionOffset({
      legWaypoints: [
        { lat: 47, lon: -120 },
        { lat: 47.5, lon: -120 }, // existing via
        { lat: 49, lon: -120 },
      ],
      geometry: straight,
      dragged: { lat: 48.5, lon: -119.99 },
    })

    expect(offset).toBe(2)
  })

  it('orders by position along the route, not by straight-line proximity', () => {
    // A route that doubles back passes close to its own start near the end. Ordering by
    // distance-to-waypoint would insert the via-point at the wrong end of the leg.
    const hairpin: Coordinate[] = [
      { lat: 47.0, lon: -120.0 },
      { lat: 47.2, lon: -120.0 },
      { lat: 47.2, lon: -120.02 },
      { lat: 47.01, lon: -120.02 }, // back down, close to the start again
    ]

    const offset = viaInsertionOffset({
      legWaypoints: [
        { lat: 47.0, lon: -120.0 },
        { lat: 47.2, lon: -120.0 }, // existing via, at the top of the climb
        { lat: 47.01, lon: -120.02 },
      ],
      // Grabbed on the *return* leg, which is spatially near the start waypoint.
      geometry: hairpin,
      dragged: { lat: 47.05, lon: -120.021 },
    })

    expect(offset).toBe(2)
  })

  it('never inserts before the start or after the end of the leg', () => {
    const legPoints = [
      { lat: 47, lon: -120 },
      { lat: 48, lon: -120 },
      { lat: 49, lon: -120 },
    ]

    const beforeStart = viaInsertionOffset({
      legWaypoints: legPoints,
      geometry: straight,
      dragged: { lat: 40, lon: -120 },
    })
    const afterEnd = viaInsertionOffset({
      legWaypoints: legPoints,
      geometry: straight,
      dragged: { lat: 60, lon: -120 },
    })

    expect(beforeStart).toBe(1)
    expect(afterEnd).toBe(2)
  })
})

describe('insertVia', () => {
  const trip = {
    waypoints: [waypoint(47), waypoint(48), waypoint(49)] as readonly Waypoint[],
    legs: [leg(0, 1, [{ lat: 47, lon: -120 }]), leg(1, 2, [{ lat: 48, lon: -120 }])] as readonly TripLeg[],
  }

  it('adds the point the user dragged to, pinned, with no name', () => {
    const result = insertVia(trip, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.5, lon: -120.1 } })

    expect(result.waypoints[1]).toEqual({
      coordinate: { lat: 47.5, lon: -120.1 },
      name: null,
      // Placed by hand, so a later replan must not move or discard it.
      pinned: true,
    })
    expect(result.waypoints).toHaveLength(4)
  })

  it('extends the dragged leg and shifts only the legs after it', () => {
    const result = insertVia(trip, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.5, lon: -120 } })

    expect(result.legs[0]?.start_waypoint_index).toBe(0)
    expect(result.legs[0]?.end_waypoint_index).toBe(2) // was 1, now spans the new via
    expect(result.legs[1]?.start_waypoint_index).toBe(2)
    expect(result.legs[1]?.end_waypoint_index).toBe(3)
    expect(result.legs).toHaveLength(2) // extended, not split
  })

  it('keeps the legs contiguous, which is what the backend rejects trips over', () => {
    const result = insertVia(trip, { legIndex: 1, offsetInLeg: 1, coordinate: { lat: 48.5, lon: -120 } })

    assertContiguous(result.legs, result.waypoints.length)
  })

  it('leaves the untouched leg exactly as it was, geometry and all', () => {
    // "Re-routing one leg never disturbs its neighbours" — including not invalidating them.
    const result = insertVia(trip, { legIndex: 1, offsetInLeg: 1, coordinate: { lat: 48.5, lon: -120 } })

    expect(result.legs[0]).toBe(trip.legs[0])
  })

  it('keeps the dragged leg’s old geometry until the new route arrives', () => {
    // The fast path renders optimistically and reconciles on the response; discarding the
    // geometry here would blank the line for the duration of the request.
    const result = insertVia(trip, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.5, lon: -120 } })

    expect(result.legs[0]?.routed).toEqual(trip.legs[0]?.routed)
  })

  it('survives being dragged twice, keeping the second point in order', () => {
    const once = insertVia(trip, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.6, lon: -120 } })
    const twice = insertVia(once, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.2, lon: -120 } })

    expect(twice.waypoints.map((point) => point.coordinate.lat)).toEqual([47, 47.2, 47.6, 48, 49])
    expect(twice.legs[0]?.end_waypoint_index).toBe(3)
    assertContiguous(twice.legs, twice.waypoints.length)
  })

  it('does not mutate the trip it was given', () => {
    insertVia(trip, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.5, lon: -120 } })

    expect(trip.waypoints).toHaveLength(3)
    expect(trip.legs[0]?.end_waypoint_index).toBe(1)
  })

  it('refuses a leg index that does not exist, rather than silently doing nothing', () => {
    // A drag that quietly no-ops is worse than one that throws: the user sees the line snap
    // back with no explanation and no way to tell it apart from a routing failure.
    expect(() => insertVia(trip, { legIndex: 5, offsetInLeg: 1, coordinate: { lat: 1, lon: 2 } })).toThrow(
      RangeError,
    )
  })
})

describe('spliceRoutedLeg', () => {
  const legs: readonly TripLeg[] = [
    leg(0, 1, [{ lat: 47, lon: -120 }]),
    leg(1, 2, [{ lat: 48, lon: -120 }]),
    leg(2, 3, [{ lat: 49, lon: -120 }]),
  ]

  it('replaces one leg’s geometry', () => {
    const fresh = routed([
      { lat: 48, lon: -120 },
      { lat: 48.5, lon: -120.2 },
    ])

    const result = spliceRoutedLeg(legs, 1, fresh)

    expect(result[1]?.routed).toBe(fresh)
    expect(result[1]?.start_waypoint_index).toBe(1)
  })

  it('leaves every neighbour untouched, by identity', () => {
    const result = spliceRoutedLeg(legs, 1, routed([{ lat: 0, lon: 0 }]))

    expect(result[0]).toBe(legs[0])
    expect(result[2]).toBe(legs[2])
  })

  it('refuses an out-of-range leg', () => {
    expect(() => spliceRoutedLeg(legs, 9, routed([{ lat: 0, lon: 0 }]))).toThrow(RangeError)
  })
})
