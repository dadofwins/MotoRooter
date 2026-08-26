import { describe, expect, it } from 'vitest'
import { routeLeg, tripLeg, waypoint } from '../api/fixtures'
import type { TripLeg, Waypoint } from '../api/types'
import {
  legsSpanning,
  withWaypointAppended,
  withLegSplit,
  withWaypointRemoved,
} from './legStructure'

/**
 * The point of all of this is that **an edit must not invalidate a leg it did not touch.**
 *
 * Every test here is really that one assertion in a different shape: a leg the rider did not
 * change keeps its geometry, by identity, so nothing re-routes it. Identity rather than value,
 * because value equality would pass for a re-created leg that a re-route has already paid for.
 */

const POINTS: readonly Waypoint[] = [
  waypoint(47.0, -122.0),
  waypoint(47.5, -120.5),
  waypoint(47.2, -120.2),
]

/** Legs with distinguishable geometry, so a swapped one is visible. */
function routedLegs(count: number): TripLeg[] {
  return Array.from({ length: count }, (_unused, index) =>
    tripLeg({
      start_waypoint_index: index,
      end_waypoint_index: index + 1,
      routed: routeLeg({ distance_m: (index + 1) * 1000 }),
    }),
  )
}

describe('legsSpanning', () => {
  it('makes one leg per consecutive pair', () => {
    const legs = legsSpanning(4, 'unpaved')

    expect(legs.map((leg) => [leg.start_waypoint_index, leg.end_waypoint_index])).toEqual([
      [0, 1],
      [1, 2],
      [2, 3],
    ])
  })

  it('joins each leg to the next at a shared waypoint', () => {
    // The backend rejects a trip whose legs are not contiguous, so this is a contract and not
    // a preference: leg N+1 starts at exactly the waypoint leg N ended on.
    const legs = legsSpanning(5, 'twisty_paved')

    legs.slice(1).forEach((leg, index) => {
      expect(leg.start_waypoint_index).toBe(legs[index]?.end_waypoint_index)
    })
  })

  it('has no legs until there are two points to route between', () => {
    expect(legsSpanning(0, 'unpaved')).toEqual([])
    expect(legsSpanning(1, 'unpaved')).toEqual([])
  })

  it('gives every leg the intent it was asked for and no geometry yet', () => {
    const legs = legsSpanning(3, 'highway_connector')

    for (const leg of legs) {
      expect(leg.intent).toBe('highway_connector')
      expect(leg.routed).toBeNull()
      expect(leg.last_routing_error).toBeNull()
    }
  })
})

describe('withWaypointAppended', () => {
  it('adds one leg and leaves the rest exactly as they were', () => {
    // This is the whole reason the branch exists. Adding a fourth point used to discard the
    // geometry of the first three, so a rider building a trip click by click re-routed the
    // entire thing on every click — the quota bill and the latency both grew with the trip.
    const legs = routedLegs(2)
    const before = { waypoints: POINTS, legs }

    const after = withWaypointAppended(before, waypoint(46.9, -119.8), 'unpaved')

    expect(after.legs).toHaveLength(3)
    expect(after.legs[0]).toBe(legs[0])
    expect(after.legs[1]).toBe(legs[1])
    expect(after.legs[2]?.routed).toBeNull()
    expect([after.legs[2]?.start_waypoint_index, after.legs[2]?.end_waypoint_index]).toEqual([2, 3])
  })

  it('makes the first leg once there is a second point', () => {
    const before = { waypoints: [waypoint(47.0, -122.0)], legs: [] }

    const after = withWaypointAppended(before, waypoint(47.5, -120.5), 'unpaved')

    expect(after.waypoints).toHaveLength(2)
    expect(after.legs).toHaveLength(1)
    expect([after.legs[0]?.start_waypoint_index, after.legs[0]?.end_waypoint_index]).toEqual([0, 1])
  })

  it('makes no leg out of the very first point', () => {
    const after = withWaypointAppended({ waypoints: [], legs: [] }, waypoint(47.0, -122.0), 'unpaved')

    expect(after.waypoints).toHaveLength(1)
    expect(after.legs).toEqual([])
  })

  it('appends after a leg the rider extended by dragging', () => {
    // A dragged leg spans three waypoints rather than two, so the new leg has to start from
    // the end of the *last leg* and not from "one before the end of the list".
    const dragged = tripLeg({ start_waypoint_index: 0, end_waypoint_index: 2 })
    const before = { waypoints: POINTS, legs: [dragged] }

    const after = withWaypointAppended(before, waypoint(46.9, -119.8), 'unpaved')

    expect(after.legs[0]).toBe(dragged)
    expect([after.legs[1]?.start_waypoint_index, after.legs[1]?.end_waypoint_index]).toEqual([2, 3])
  })
})

describe('withWaypointRemoved', () => {
  it('merges the two legs that met at the removed waypoint', () => {
    const legs = routedLegs(2)

    const after = withWaypointRemoved({ waypoints: POINTS, legs }, 1)

    expect(after.waypoints).toHaveLength(2)
    expect(after.legs).toHaveLength(1)
    expect([after.legs[0]?.start_waypoint_index, after.legs[0]?.end_waypoint_index]).toEqual([0, 1])
    // The merged leg connects two places it has never been routed between, so keeping either
    // half's geometry would draw a line through a waypoint that is gone.
    expect(after.legs[0]?.routed).toBeNull()
  })

  it('keeps the intent of the leg the rider was on', () => {
    // Merging is not a fresh choice of mode. The first half's intent wins because that is the
    // segment the removed point was inside; inventing a default here would silently retarmac
    // someone's dirt section.
    const legs = [
      tripLeg({ intent: 'unpaved', start_waypoint_index: 0, end_waypoint_index: 1 }),
      tripLeg({ intent: 'highway_connector', start_waypoint_index: 1, end_waypoint_index: 2 }),
    ]

    const after = withWaypointRemoved({ waypoints: POINTS, legs }, 1)

    expect(after.legs[0]?.intent).toBe('unpaved')
  })

  it('drops the first leg when the start is removed', () => {
    const legs = routedLegs(2)

    const after = withWaypointRemoved({ waypoints: POINTS, legs }, 0)

    expect(after.legs).toHaveLength(1)
    // What was the second leg, re-indexed: its geometry still describes the same two places,
    // so it must survive.
    expect(after.legs[0]?.routed).toBe(legs[1]?.routed)
    expect([after.legs[0]?.start_waypoint_index, after.legs[0]?.end_waypoint_index]).toEqual([0, 1])
  })

  it('drops the last leg when the end is removed', () => {
    const legs = routedLegs(2)

    const after = withWaypointRemoved({ waypoints: POINTS, legs }, 2)

    expect(after.legs).toHaveLength(1)
    expect(after.legs[0]).toBe(legs[0])
  })

  it('leaves untouched legs alone when removing from the middle of a longer trip', () => {
    const legs = routedLegs(4)
    const points = [...POINTS, waypoint(46.9, -119.8), waypoint(46.5, -119.2)]

    const after = withWaypointRemoved({ waypoints: points, legs }, 2)

    expect(after.legs).toHaveLength(3)
    // Before the change: identical, including its geometry.
    expect(after.legs[0]).toBe(legs[0])
    // The merge of legs 1 and 2, which is the only thing that needs re-routing. Old waypoints
    // 1..3, which are new 1..2 once the removal has shifted everything after it down.
    expect(after.legs[1]?.routed).toBeNull()
    expect([after.legs[1]?.start_waypoint_index, after.legs[1]?.end_waypoint_index]).toEqual([1, 2])
    // After the change: same geometry, shifted indices. Re-routing it would be wasted quota.
    expect(after.legs[2]?.routed).toBe(legs[3]?.routed)
    expect([after.legs[2]?.start_waypoint_index, after.legs[2]?.end_waypoint_index]).toEqual([2, 3])
  })

  it('shrinks a leg the rider had extended, rather than merging its neighbours', () => {
    // Removing a via-point from inside a three-waypoint leg leaves one leg, not none: the
    // waypoint was interior to it, so no boundary moved and no neighbour is involved.
    const legs = [
      tripLeg({ start_waypoint_index: 0, end_waypoint_index: 2, routed: routeLeg() }),
      tripLeg({ start_waypoint_index: 2, end_waypoint_index: 3, routed: routeLeg() }),
    ]
    const points = [...POINTS, waypoint(46.9, -119.8)]

    const after = withWaypointRemoved({ waypoints: points, legs }, 1)

    expect(after.legs).toHaveLength(2)
    expect([after.legs[0]?.start_waypoint_index, after.legs[0]?.end_waypoint_index]).toEqual([0, 1])
    // Its shape changed, so its geometry is a lie.
    expect(after.legs[0]?.routed).toBeNull()
    // The next leg never touched that waypoint and keeps what it had.
    expect(after.legs[1]?.routed).toBe(legs[1]?.routed)
  })

  it('leaves nothing to route when a two-point trip loses a point', () => {
    const legs = routedLegs(1)

    const after = withWaypointRemoved({ waypoints: POINTS.slice(0, 2), legs }, 1)

    expect(after.waypoints).toHaveLength(1)
    expect(after.legs).toEqual([])
  })

  it('refuses an index that is not a waypoint', () => {
    // Louder than a no-op: a silent one produces legs pointing at waypoints that do not
    // exist, which the backend rejects on save long after the cause is gone.
    const before = { waypoints: POINTS, legs: routedLegs(2) }

    expect(() => withWaypointRemoved(before, 3)).toThrow(RangeError)
    expect(() => withWaypointRemoved(before, -1)).toThrow(RangeError)
  })

  it('always leaves the legs contiguous and covering every waypoint', () => {
    // The invariant the backend enforces, checked over every removal position rather than at
    // the one or two that happened to be interesting.
    const points = [...POINTS, waypoint(46.9, -119.8), waypoint(46.5, -119.2)]

    for (let index = 0; index < points.length; index++) {
      const after = withWaypointRemoved({ waypoints: points, legs: routedLegs(4) }, index)

      expect(after.legs[0]?.start_waypoint_index).toBe(0)
      expect(after.legs.at(-1)?.end_waypoint_index).toBe(after.waypoints.length - 1)
      after.legs.slice(1).forEach((leg, previous) => {
        expect(leg.start_waypoint_index).toBe(after.legs[previous]?.end_waypoint_index)
      })
    }
  })
})

/**
 * Dividing one segment into two.
 *
 * Tim: *"if you right click the route the menu has an 'add point' option which just adds a point
 * where you clicked... the idea being you can use that to change routing options more granularly
 * — like maybe you want to keep part of a dirt section but route via road closer to it."*
 *
 * That last clause is the whole request, and it rules out `insertVia`. A via *grows* a leg, so
 * the result is one leg with one intent — exactly what he is trying to get away from. Splitting
 * gives two legs that can carry different modes, which is what "more granularly" means.
 *
 * This is the inverse of the merge `withWaypointRemoved` performs, and the two should stay
 * inverses: split at a point, remove that point, and you are back where you started.
 */
describe('withLegSplit', () => {
  function routed(distanceM: number) {
    return routeLeg({ distance_m: distanceM })
  }

  const three: readonly Waypoint[] = [
    waypoint(47.0, -120.0),
    waypoint(47.5, -120.5),
    waypoint(48.0, -121.0),
  ]

  it('turns one leg into two at the new point', () => {
    const before = {
      waypoints: three.slice(0, 2),
      legs: [tripLeg({ start_waypoint_index: 0, end_waypoint_index: 1, routed: routed(1000) })],
    }

    const after = withLegSplit(before, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.25, lon: -120.25 } })

    expect(after.waypoints).toHaveLength(3)
    expect(after.legs.map((leg) => [leg.start_waypoint_index, leg.end_waypoint_index])).toEqual([
      [0, 1],
      [1, 2],
    ])
  })

  it('gives both halves the intent the original had', () => {
    // Splitting is not choosing. The rider divides the segment and then picks a mode for one
    // half; inventing a different mode for either would be answering a question they have not
    // asked yet.
    const before = {
      waypoints: three.slice(0, 2),
      legs: [tripLeg({ start_waypoint_index: 0, end_waypoint_index: 1, intent: 'unpaved' })],
    }

    const after = withLegSplit(before, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.25, lon: -120.25 } })

    expect(after.legs.map((leg) => leg.intent)).toEqual(['unpaved', 'unpaved'])
  })

  it('leaves both halves needing a route', () => {
    // One polyline cannot describe two legs, and each half is a road nobody has asked about.
    const before = {
      waypoints: three.slice(0, 2),
      legs: [tripLeg({ start_waypoint_index: 0, end_waypoint_index: 1, routed: routed(1000) })],
    }

    const after = withLegSplit(before, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.25, lon: -120.25 } })

    expect(after.legs.every((leg) => leg.routed === null)).toBe(true)
  })

  it('leaves every other leg exactly as it was', () => {
    const legs = [
      tripLeg({ start_waypoint_index: 0, end_waypoint_index: 1, routed: routed(1000) }),
      tripLeg({ start_waypoint_index: 1, end_waypoint_index: 2, routed: routed(2000) }),
    ]

    const after = withLegSplit(
      { waypoints: three, legs },
      { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.25, lon: -120.25 } },
    )

    expect(after.legs).toHaveLength(3)
    // The untouched leg keeps its geometry, by identity, and shifts index only.
    expect(after.legs[2]?.routed).toBe(legs[1]?.routed)
    expect([after.legs[2]?.start_waypoint_index, after.legs[2]?.end_waypoint_index]).toEqual([2, 3])
  })

  it('splits a leg a drag has already grown, at the position asked for', () => {
    // A leg spanning three waypoints has two places to divide it, and the offset says which.
    const legs = [tripLeg({ start_waypoint_index: 0, end_waypoint_index: 2, intent: 'unpaved' })]

    const after = withLegSplit(
      { waypoints: three, legs },
      { legIndex: 0, offsetInLeg: 2, coordinate: { lat: 47.9, lon: -120.9 } },
    )

    expect(after.legs.map((leg) => [leg.start_waypoint_index, leg.end_waypoint_index])).toEqual([
      [0, 2],
      [2, 3],
    ])
  })

  it('pins the new point, because the rider placed it', () => {
    const before = {
      waypoints: three.slice(0, 2),
      legs: [tripLeg({ start_waypoint_index: 0, end_waypoint_index: 1 })],
    }

    const after = withLegSplit(before, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.25, lon: -120.25 } })

    expect(after.waypoints[1]?.pinned).toBe(true)
  })

  it('refuses an offset that would not divide anything', () => {
    // Offset 0 is the leg's own start and the previous leg's end; splitting there produces an
    // empty half and a trip that saves cleanly while being nonsense.
    const before = {
      waypoints: three.slice(0, 2),
      legs: [tripLeg({ start_waypoint_index: 0, end_waypoint_index: 1 })],
    }

    expect(() =>
      withLegSplit(before, { legIndex: 0, offsetInLeg: 0, coordinate: { lat: 47.1, lon: -120.1 } }),
    ).toThrow(RangeError)
  })

  it('is the inverse of the merge that removing a point performs', () => {
    // The property that keeps the two operations honest with each other.
    const before = {
      waypoints: three.slice(0, 2),
      legs: [tripLeg({ start_waypoint_index: 0, end_waypoint_index: 1, routed: null })],
    }

    const split = withLegSplit(before, { legIndex: 0, offsetInLeg: 1, coordinate: { lat: 47.25, lon: -120.25 } })
    const merged = withWaypointRemoved(split, 1)

    expect(merged.waypoints).toEqual(before.waypoints)
    expect(merged.legs.map((leg) => [leg.start_waypoint_index, leg.end_waypoint_index])).toEqual([
      [0, 1],
    ])
  })
})
