import { describe, expect, it } from 'vitest'
import { summariseSurface, toWholePercentages } from './surfaceSummary'
import type { Coordinate, RouteLeg, TripLeg } from '../api/types'
import { routeLeg } from '../api/fixtures'

/**
 * How much of a route is dirt, tarmac, and unsurveyed.
 *
 * The decision this exists to honour: unknown surface stays unknown. `unpaved_fraction`
 * counts only what is explicitly tagged, and on a real route roughly a third of the distance
 * has no OSM surface tag at all — so "41% unpaved" alone lets the rest read as tarmac. Three
 * numbers, and the third is not allowed to disappear.
 *
 * Fractions come from the geometry, because that is the only place surface lives. The
 * kilometres are those fractions applied to the provider's own distance, so the summary
 * agrees with the total shown beside it rather than quietly disagreeing by a percent.
 */

/** Points spaced evenly along a meridian, so each edge is the same length. */
function evenLine(points: number): Coordinate[] {
  return Array.from({ length: points }, (_, index) => ({ lat: 47 + index * 0.01, lon: -120 }))
}

function leg(
  geometry: Coordinate[],
  spans: RouteLeg['surface_spans'] = [],
  distanceM = 1000,
): TripLeg {
  return {
    intent: 'unpaved',
    start_waypoint_index: 0,
    end_waypoint_index: 1,
    provider_override: null,
    routed: routeLeg({
      geometry,
      distance_m: distanceM,
      provider: 'ors',
      surface_spans: spans,
    }),
  }
}

describe('summariseSurface', () => {
  it('has nothing to say about a route that does not exist yet', () => {
    expect(summariseSurface([])).toBeNull()
    expect(summariseSurface([leg([])])).toBeNull()
  })

  it('counts an entirely untagged route as entirely unsurveyed', () => {
    // Not as paved. Absence of data is not evidence of tarmac, and this is exactly the case
    // that produced a uniformly grey line on a real route.
    const summary = summariseSurface([leg(evenLine(5))])

    expect(summary?.fractions.unknown).toBeCloseTo(1, 6)
    expect(summary?.fractions.paved).toBe(0)
    expect(summary?.fractions.unpaved).toBe(0)
  })

  it('splits by surface along the geometry', () => {
    // Four equal edges: two paved, one unpaved, one untagged.
    const summary = summariseSurface([
      leg(evenLine(5), [
        { start_index: 0, end_index: 2, surface: 'paved' },
        { start_index: 2, end_index: 3, surface: 'unpaved' },
      ]),
    ])

    expect(summary?.fractions.paved).toBeCloseTo(0.5, 6)
    expect(summary?.fractions.unpaved).toBeCloseTo(0.25, 6)
    expect(summary?.fractions.unknown).toBeCloseTo(0.25, 6)
  })

  it('always accounts for the whole route', () => {
    const summary = summariseSurface([
      leg(evenLine(7), [{ start_index: 1, end_index: 4, surface: 'unpaved' }]),
    ])
    const total =
      (summary?.fractions.paved ?? 0) +
      (summary?.fractions.unpaved ?? 0) +
      (summary?.fractions.unknown ?? 0)

    expect(total).toBeCloseTo(1, 6)
  })

  it('adds up across legs, weighting each by its own length', () => {
    // A short unpaved leg and a long paved one must not count equally.
    const shortDirt = leg(evenLine(2), [{ start_index: 0, end_index: 1, surface: 'unpaved' }], 1000)
    const longRoad = leg(
      Array.from({ length: 2 }, (_, index) => ({ lat: 47 + index * 0.09, lon: -120 })),
      [{ start_index: 0, end_index: 1, surface: 'paved' }],
      9000,
    )

    const summary = summariseSurface([shortDirt, longRoad])

    expect(summary?.fractions.unpaved).toBeCloseTo(0.1, 2)
    expect(summary?.fractions.paved).toBeCloseTo(0.9, 2)
  })

  it('reports distances against the provider’s own total, not its own arithmetic', () => {
    // The rail shows the route distance elsewhere. A summary that measured the polyline
    // itself would disagree with it by a percent or two and look like a bug.
    const summary = summariseSurface([
      leg(evenLine(5), [{ start_index: 0, end_index: 2, surface: 'paved' }], 40_000),
    ])

    expect(summary?.totalM).toBe(40_000)
    expect(summary?.distanceM.paved).toBeCloseTo(20_000, 0)
  })

  it('ignores a leg with no route yet, rather than counting it as unsurveyed', () => {
    // An unrouted leg has no length and no surface; treating it as unknown would invent
    // distance that is not there.
    const summary = summariseSurface([
      leg(evenLine(3), [{ start_index: 0, end_index: 2, surface: 'paved' }], 5000),
      { ...leg([]), routed: null },
    ])

    expect(summary?.fractions.paved).toBeCloseTo(1, 6)
    expect(summary?.totalM).toBe(5000)
  })
})

describe('toWholePercentages', () => {
  it('always adds up to 100', () => {
    // A reader who adds three numbers and gets 99 has found a bug, whether or not there is
    // one. Rounding each share independently produces exactly that.
    const thirds = toWholePercentages({ paved: 1 / 3, unpaved: 1 / 3, unknown: 1 / 3 })

    expect(thirds.paved + thirds.unpaved + thirds.unknown).toBe(100)
  })

  it('gives the spare point to the share with the strongest claim', () => {
    const shares = toWholePercentages({ unpaved: 0.396, paved: 0.322, unknown: 0.282 })

    expect(shares).toEqual({ unpaved: 40, paved: 32, unknown: 28 })
    expect(shares.unpaved + shares.paved + shares.unknown).toBe(100)
  })

  it('holds up on an exact split and on an all-or-nothing route', () => {
    expect(toWholePercentages({ paved: 0.5, unpaved: 0.25, unknown: 0.25 })).toEqual({
      paved: 50,
      unpaved: 25,
      unknown: 25,
    })
    expect(toWholePercentages({ paved: 0, unpaved: 0, unknown: 1 })).toEqual({
      paved: 0,
      unpaved: 0,
      unknown: 100,
    })
  })
})
