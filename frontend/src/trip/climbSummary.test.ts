import { describe, expect, it } from 'vitest'
import { climbSummary } from './climbSummary'
import { routeLeg, tripLeg } from '../api/fixtures'

/**
 * How much a trip climbs, and how much of it nobody measured.
 *
 * Climb was suppressed for months because ORS reported 6,400-8,800 m against a published 3,188 m.
 * That is now explained — `cycling-mountain` returned an exact 0 where its elevation lookup
 * failed, and twelve such points in 2,763 accounted for 3,124 m of the gap, because each plunge to
 * sea level and back adds twice the local elevation to a cumulative sum. So the figure is now
 * trustworthy and worth showing: a 3,600 m day and a 1,500 m day are different rides over the same
 * distance, which is exactly the kind of thing an adventure rider plans around.
 *
 * **But it is only measured where the engine does elevation at all.** Google reports none, so on a
 * mixed trip — highway, dirt, highway, which is the shape of Tim's own test route — climb exists
 * for 40 km of 269. Presenting that as *the* trip's climb would understate it by a factor of six.
 *
 * So this reports the unmeasured distance alongside the figure. Same rule as surface, and for the
 * same reason: unknown stays unknown rather than being folded into a number that looks complete.
 */

function withAscent(ascentM: number | null, distanceM: number) {
  return tripLeg({ routed: routeLeg({ ascent_m: ascentM, distance_m: distanceM }) })
}

describe('climbSummary', () => {
  it('adds up the climb of the legs that measured it', () => {
    const summary = climbSummary([withAscent(800, 20_000), withAscent(400, 20_000)])

    expect(summary.ascentM).toBe(1200)
    expect(summary.unmeasuredDistanceM).toBe(0)
  })

  it('reports how far went unmeasured rather than counting it as flat', () => {
    // The mixed-trip case, and the whole reason this returns two numbers. Google reports no
    // elevation, so those legs are not zero-climb — they are unknown-climb.
    const summary = climbSummary([
      withAscent(null, 176_800), // highway, Google
      withAscent(1200, 40_300), // dirt, ORS
      withAscent(null, 52_100), // twisties, Google
    ])

    expect(summary.ascentM).toBe(1200)
    expect(summary.unmeasuredDistanceM).toBe(228_900)
  })

  it('has nothing to report when no leg measured anything', () => {
    // Null rather than zero. A trip routed entirely through an engine without elevation has no
    // climb figure at all, and zero would be a claim.
    const summary = climbSummary([withAscent(null, 20_000), withAscent(null, 20_000)])

    expect(summary.ascentM).toBeNull()
  })

  it('has nothing to report for a trip with no routed legs', () => {
    expect(climbSummary([]).ascentM).toBeNull()
    expect(climbSummary([tripLeg({ routed: null })]).ascentM).toBeNull()
  })

  it('treats a leg that genuinely climbs nothing as measured', () => {
    // Zero is a real answer on flat ground, and the sentinel that used to make zero suspicious
    // is now filtered inside the adapter. Folding it in with the unmeasured legs would lose the
    // distinction the field now carries honestly.
    const summary = climbSummary([withAscent(0, 20_000), withAscent(500, 20_000)])

    expect(summary.ascentM).toBe(500)
    expect(summary.unmeasuredDistanceM).toBe(0)
  })

  it('ignores a leg with no geometry, which has no distance either', () => {
    const summary = climbSummary([withAscent(600, 20_000), tripLeg({ routed: null })])

    expect(summary.ascentM).toBe(600)
    expect(summary.unmeasuredDistanceM).toBe(0)
  })

  it('says what share of the distance the figure covers', () => {
    // So a caller can decide whether the number is worth showing at all rather than re-deriving
    // the same ratio at every call site.
    const summary = climbSummary([withAscent(1200, 40_000), withAscent(null, 160_000)])

    expect(summary.measuredDistanceM).toBe(40_000)
    expect(summary.measuredFraction).toBeCloseTo(0.2, 5)
  })

  it('reports a fraction of zero rather than dividing by nothing', () => {
    expect(climbSummary([]).measuredFraction).toBe(0)
  })
})
