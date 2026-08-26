import { describe, expect, it } from 'vitest'
import { coordinateAt, pixelsAt } from './cluster'
import { FAN_MAX_MEMBERS, FAN_SPACING_PX, fanPositions } from './fan'

/**
 * Opening a small group out into its members.
 *
 * Tim's call, after the measurement said a list was the only thing that worked at twelve: fan up
 * to eight, list beyond that. The fan is the better answer where it fits, because it keeps the
 * places on the map instead of moving them into a panel — and it only fits while the pins have
 * room to be pins.
 *
 * A fanned pin is **not where its place is**, which is the honest cost of the whole idea. What
 * keeps that from being a lie is the leader line back to the group, so the offset is drawn rather
 * than hidden.
 */

const CENTRE = { lat: 47.5, lon: -120.5 }
const ZOOM = 12

/** How far apart, in pixels at this zoom, two fanned positions sit. */
function gapPx(from: { lat: number; lon: number }, to: { lat: number; lon: number }): number {
  const a = pixelsAt(from, ZOOM)
  const b = pixelsAt(to, ZOOM)
  return Math.hypot(b.x - a.x, b.y - a.y)
}

describe('coordinateAt', () => {
  it('undoes the projection, so a pixel offset can become a place to draw', () => {
    // The fan is reasoned about in pixels and drawn in coordinates. A sign error here would put
    // every fanned pin in the wrong hemisphere of the group and nothing else would notice.
    const there = coordinateAt(pixelsAt(CENTRE, ZOOM), ZOOM)

    expect(there.lat).toBeCloseTo(CENTRE.lat, 9)
    expect(there.lon).toBeCloseTo(CENTRE.lon, 9)
  })

  it('round-trips away from the equator, where the projection stretches', () => {
    const arctic = { lat: 68.4, lon: 17.2 }

    const there = coordinateAt(pixelsAt(arctic, ZOOM), ZOOM)

    expect(there.lat).toBeCloseTo(arctic.lat, 9)
    expect(there.lon).toBeCloseTo(arctic.lon, 9)
  })
})

describe('fanPositions', () => {
  it('gives one position per member', () => {
    expect(fanPositions(CENTRE, 5, ZOOM)).toHaveLength(5)
  })

  it('keeps the pins clear of each other at the largest group it will fan', () => {
    // The reason there is a ceiling at all. Twelve pins on a fixed 40px radius sit 21px apart —
    // narrower than a pin, so the fan would recreate the overlap it exists to solve.
    const placed = fanPositions(CENTRE, FAN_MAX_MEMBERS, ZOOM)

    for (let index = 0; index < placed.length; index += 1) {
      const next = placed[(index + 1) % placed.length]
      const here = placed[index]
      if (here === undefined || next === undefined) throw new Error('missing position')
      expect(gapPx(here, next)).toBeGreaterThanOrEqual(FAN_SPACING_PX)
    }
  })

  it('spreads wider rather than crowding when the ceiling is raised', () => {
    // `FAN_MAX_MEMBERS` is deliberately easy to move, so the geometry has to hold above today's
    // value or raising it would quietly reintroduce the overlap the fan exists to avoid. Twelve
    // is the largest group a real corridor produced.
    const placed = fanPositions(CENTRE, 12, ZOOM)

    for (let index = 0; index < placed.length; index += 1) {
      const here = placed[index]
      const next = placed[(index + 1) % placed.length]
      if (here === undefined || next === undefined) throw new Error('missing position')
      expect(gapPx(here, next)).toBeGreaterThanOrEqual(FAN_SPACING_PX)
    }
  })

  it('keeps the pins clear of each other at the smallest group too', () => {
    const [first, second] = fanPositions(CENTRE, 2, ZOOM)
    if (first === undefined || second === undefined) throw new Error('missing position')

    expect(gapPx(first, second)).toBeGreaterThanOrEqual(FAN_SPACING_PX)
  })

  it('holds them close enough that the group is still where the places are', () => {
    // The other half of the trade. A radius grown until everything fits would put a pin a
    // kilometre from the place it stands for, and the leader line would be drawing a lie
    // rather than disclosing an offset.
    for (const count of [2, 4, 6, FAN_MAX_MEMBERS]) {
      for (const placed of fanPositions(CENTRE, count, ZOOM)) {
        expect(gapPx(CENTRE, placed)).toBeLessThan(80)
      }
    }
  })

  it('puts the first one straight up, so a group of two is not a diagonal', () => {
    const [first] = fanPositions(CENTRE, 2, ZOOM)
    if (first === undefined) throw new Error('missing position')

    expect(first.lon).toBeCloseTo(CENTRE.lon, 9)
    expect(first.lat).toBeGreaterThan(CENTRE.lat)
  })

  it('spaces them evenly around the group', () => {
    const placed = fanPositions(CENTRE, 4, ZOOM)
    const gaps = placed.map((each, index) => {
      const next = placed[(index + 1) % placed.length]
      if (next === undefined) throw new Error('missing position')
      return gapPx(each, next)
    })

    for (const gap of gaps) expect(gap).toBeCloseTo(gaps[0] ?? 0, 6)
  })

  it('gives the same answer twice, so a fan does not shuffle while it is open', () => {
    expect(fanPositions(CENTRE, 6, ZOOM)).toEqual(fanPositions(CENTRE, 6, ZOOM))
  })

  it('scales with the zoom, so the fan is the same size on screen at any of them', () => {
    // In pixels the fan is fixed; in degrees it must shrink as the rider zooms in, or it would
    // fling its members across the county.
    const near = gapPx(CENTRE, fanPositions(CENTRE, 4, ZOOM)[0] ?? CENTRE)
    const wide = pixelsAt(CENTRE, 16)
    const at16 = pixelsAt(fanPositions(CENTRE, 4, 16)[0] ?? CENTRE, 16)

    expect(Math.hypot(at16.x - wide.x, at16.y - wide.y)).toBeCloseTo(near, 6)
  })

  it('has nothing to place for a group of none', () => {
    expect(fanPositions(CENTRE, 0, ZOOM)).toEqual([])
  })
})
