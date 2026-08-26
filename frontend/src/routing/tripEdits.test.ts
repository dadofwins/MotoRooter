import { describe, expect, it } from 'vitest'
import {
  addPoiToRoute,
  insertVia,
  isLegStale,
  legWaypoints,
  spliceRoutedLeg,
  viaInsertionOffset,
} from './tripEdits'
import type { RouteEdit } from './tripEdits'
import type { Coordinate, Poi, RouteLeg, TripLeg, Waypoint } from '../api/types'
import { routeLeg } from '../api/fixtures'

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
  return routeLeg({ geometry: [...geometry] })
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

  it('refuses an offset that would move a neighbouring leg’s endpoint', () => {
    // Offset 0 inserts at the leg's own start index, which is the *previous* leg's end.
    // The result is still contiguous and saves cleanly, so nothing downstream catches it:
    // the user drags one leg and the one before it silently changes shape.
    expect(() =>
      insertVia(trip, { legIndex: 1, offsetInLeg: 0, coordinate: { lat: 48.5, lon: -120 } }),
    ).toThrow(RangeError)
  })

  it('refuses an offset past the end of the leg, which would land the via in the next one', () => {
    // Leg 0 spans waypoints 0..1, so offset 2 is beyond it. Also saves cleanly.
    expect(() =>
      insertVia(trip, { legIndex: 0, offsetInLeg: 2, coordinate: { lat: 47.5, lon: -120 } }),
    ).toThrow(RangeError)
  })

  it('accepts every offset that is genuinely inside the leg', () => {
    const wide = {
      waypoints: [waypoint(47), waypoint(48), waypoint(49), waypoint(50)] as readonly Waypoint[],
      legs: [leg(0, 2), leg(2, 3)] as readonly TripLeg[],
    }

    for (const offsetInLeg of [1, 2]) {
      expect(() =>
        insertVia(wide, { legIndex: 0, offsetInLeg, coordinate: { lat: 47.5, lon: -120 } }),
      ).not.toThrow()
    }
  })

  it('refuses a leg index that does not exist, rather than silently doing nothing', () => {
    // A drag that quietly no-ops is worse than one that throws: the user sees the line snap
    // back with no explanation and no way to tell it apart from a routing failure.
    expect(() => insertVia(trip, { legIndex: 5, offsetInLeg: 1, coordinate: { lat: 1, lon: 2 } })).toThrow(
      RangeError,
    )
  })
})

describe('isLegStale', () => {
  /**
   * `insertVia` keeps the dragged leg's old geometry on purpose, so the line does not blink
   * out while the new route is fetched. That leaves a leg whose geometry no longer matches
   * its waypoints, which is safe only as long as the caller overwrites it. Rather than rely
   * on call order, the backend records the request each leg was routed from, so staleness
   * is a property of the data.
   */
  const waypoints = [waypoint(47), waypoint(48)]

  function fingerprinted(from: readonly Coordinate[], intent: TripLeg['intent'] = 'unpaved'): TripLeg {
    return {
      intent,
      start_waypoint_index: 0,
      end_waypoint_index: 1,
      provider_override: null,
      routed: { ...routed([{ lat: 47, lon: -120 }]), routed_from: { intent, waypoints: [...from] } },
    }
  }

  it('is fresh when the leg was routed from exactly these waypoints', () => {
    const leg = fingerprinted([
      { lat: 47, lon: -120 },
      { lat: 48, lon: -120 },
    ])

    expect(isLegStale(waypoints, leg)).toBe(false)
  })

  it('is stale once a via-point has been inserted', () => {
    // Precisely the state insertVia leaves behind mid-drag.
    const leg = fingerprinted([
      { lat: 47, lon: -120 },
      { lat: 48, lon: -120 },
    ])
    const dragged = insertVia({ waypoints, legs: [leg] }, {
      legIndex: 0,
      offsetInLeg: 1,
      coordinate: { lat: 47.5, lon: -120.2 },
    })

    expect(isLegStale(dragged.waypoints, dragged.legs[0]!)).toBe(true)
  })

  it('is stale when the intent changed, even with the same waypoints', () => {
    const leg = fingerprinted(
      [
        { lat: 47, lon: -120 },
        { lat: 48, lon: -120 },
      ],
      'unpaved',
    )

    expect(isLegStale(waypoints, { ...leg, intent: 'technical_offroad' })).toBe(true)
  })

  it('treats a leg with no geometry as stale, and one with no fingerprint as unknowable', () => {
    const unrouted: TripLeg = { ...fingerprinted([]), routed: null }
    expect(isLegStale(waypoints, unrouted)).toBe(true)

    // Older documents predate the fingerprint. Calling them fresh would hide a real
    // mismatch; calling them stale would re-route every leg of every loaded trip. Reported
    // as stale is the safer of the two, and the honest one.
    const noFingerprint: TripLeg = {
      ...fingerprinted([]),
      routed: { ...routed([{ lat: 47, lon: -120 }]), routed_from: null },
    }
    expect(isLegStale(waypoints, noFingerprint)).toBe(true)
  })

  it('tolerates the rounding the backend applies to the fingerprint, but not a real move', () => {
    // The fingerprint rounds to five decimal places (~1.1 m), so an exact comparison would
    // call every leg stale. A waypoint the rider actually moved must still register.
    const rounded = fingerprinted([
      { lat: 47.000004, lon: -120.000004 }, // within half a rounding step
      { lat: 48.000004, lon: -120.000004 },
    ])
    const moved = fingerprinted([
      { lat: 47.0005, lon: -120 }, // ~55 m: a drag, not jitter
      { lat: 48, lon: -120 },
    ])

    expect(isLegStale(waypoints, rounded)).toBe(false)
    expect(isLegStale(waypoints, moved)).toBe(true)
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

describe('addPoiToRoute', () => {
  /**
   * The mouse path for putting a discovered place on the route — and the same function the
   * assistant's tool will call, so the two cannot drift.
   *
   * A POI is inserted where it belongs *along* the route rather than tacked onto the end:
   * adding a campground halfway through a trip should not send the rider back for it.
   */
  const trip: RouteEdit = {
    waypoints: [waypoint(47), waypoint(48), waypoint(49)],
    legs: [
      leg(0, 1, [
        { lat: 47, lon: -120 },
        { lat: 47.5, lon: -120 },
        { lat: 48, lon: -120 },
      ]),
      leg(1, 2, [
        { lat: 48, lon: -120 },
        { lat: 48.5, lon: -120 },
        { lat: 49, lon: -120 },
      ]),
    ],
  }

  function poi(overrides: Partial<Poi> = {}): Poi {
    return {
      id: 'poi-1',
      name: 'Lone Fir Campground',
      category: 'campground',
      coordinate: { lat: 48.5, lon: -120.02 },
      source: 'places',
      place_id: 'ChIJ123',
      note: null,
      on_route: false,
      ...overrides,
    }
  }

  it('inserts the place into whichever leg runs nearest to it', () => {
    const result = addPoiToRoute(trip, poi())

    // Nearest to the second leg, so it goes there rather than at the end of the trip.
    expect(result?.waypoints.map((point) => point.coordinate.lat)).toEqual([47, 48, 48.5, 49])
    expect(result?.legs[1]?.end_waypoint_index).toBe(3)
    expect(result?.legs[0]).toBe(trip.legs[0]) // untouched
  })

  it('names the waypoint after the place, so the route reads back', () => {
    const result = addPoiToRoute(trip, poi())

    expect(result?.waypoints[2]).toEqual({
      coordinate: { lat: 48.5, lon: -120.02 },
      name: 'Lone Fir Campground',
      // Deliberately chosen, so a replan must not move or drop it.
      pinned: true,
    })
  })

  it('refuses an unverified suggestion, which the backend would reject anyway', () => {
    // An LLM-suggested place with no resolved place_id is a claim, not somewhere to ride to.
    expect(addPoiToRoute(trip, poi({ source: 'llm_suggested', place_id: null }))).toBeNull()
  })

  it('has nowhere to put it when there is no routed leg yet', () => {
    expect(addPoiToRoute({ waypoints: [], legs: [] }, poi())).toBeNull()
    expect(addPoiToRoute({ waypoints: [waypoint(47)], legs: [leg(0, 1)] }, poi())).toBeNull()
  })

  it('keeps the legs contiguous, as every other edit must', () => {
    const result = addPoiToRoute(trip, poi())

    assertContiguous(result?.legs ?? [], result?.waypoints.length ?? 0)
  })
})
