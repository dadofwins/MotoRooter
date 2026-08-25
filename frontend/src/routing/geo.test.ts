import { describe, expect, it } from 'vitest'
import { distanceM, nearestPointOnPath } from './geo'
import type { Coordinate } from '../api/types'

/**
 * Geometry for the drag interaction.
 *
 * `nearestPointOnPath` is the first step of every drag: the handle the user grabbed has to
 * become a position on the route line before it can become a via-point. Getting it wrong is
 * subtly awful — the via-point lands on the wrong part of the route and the leg reroutes
 * somewhere the user did not point at.
 *
 * The case that matters most for this app is a **switchback**, where the nearest *vertex* and
 * the nearest *segment* are different things. A route that prefers twisties is full of them.
 */

const SEATTLE: Coordinate = { lat: 47.6, lon: -122.33 }

describe('distanceM', () => {
  it('measures a tenth of a degree of latitude as about 11 km', () => {
    const north = { lat: SEATTLE.lat + 0.1, lon: SEATTLE.lon }

    expect(distanceM(SEATTLE, north)).toBeCloseTo(11_100, -2)
  })

  it('accounts for longitude lines converging away from the equator', () => {
    // A degree of longitude is ~111 km at the equator and ~75 km at 47.6°N. A naive
    // implementation that treats degrees as square would put a via-point in the wrong place.
    const equator = { lat: 0, lon: 0 }
    const equatorEast = { lat: 0, lon: 1 }
    const northEast = { lat: 47.6, lon: SEATTLE.lon + 1 }

    expect(distanceM(equator, equatorEast)).toBeCloseTo(111_300, -3)
    expect(distanceM({ lat: 47.6, lon: SEATTLE.lon }, northEast)).toBeCloseTo(75_000, -3)
  })

  it('is zero for a point and itself, and never negative', () => {
    expect(distanceM(SEATTLE, SEATTLE)).toBe(0)
    expect(distanceM(SEATTLE, { lat: 47.5, lon: -122.4 })).toBeGreaterThan(0)
  })
})

describe('nearestPointOnPath', () => {
  it('finds an exact vertex hit at zero distance', () => {
    const path: Coordinate[] = [
      { lat: 47.0, lon: -120.0 },
      { lat: 47.1, lon: -120.0 },
      { lat: 47.2, lon: -120.0 },
    ]

    const result = nearestPointOnPath(path, { lat: 47.1, lon: -120.0 })

    expect(result?.distanceM).toBeCloseTo(0, 6)
    expect(result?.coordinate.lat).toBeCloseTo(47.1, 9)
  })

  it('projects onto the middle of a segment', () => {
    const path: Coordinate[] = [
      { lat: 47.0, lon: -120.0 },
      { lat: 47.2, lon: -120.0 },
    ]

    // Due east of the segment's midpoint: the projection is the midpoint itself.
    const result = nearestPointOnPath(path, { lat: 47.1, lon: -119.99 })

    expect(result?.segmentIndex).toBe(0)
    expect(result?.t).toBeCloseTo(0.5, 3)
    expect(result?.coordinate.lat).toBeCloseTo(47.1, 4)
  })

  it('clamps to the ends rather than extrapolating past them', () => {
    const path: Coordinate[] = [
      { lat: 47.0, lon: -120.0 },
      { lat: 47.1, lon: -120.0 },
    ]

    const before = nearestPointOnPath(path, { lat: 46.0, lon: -120.0 })
    const after = nearestPointOnPath(path, { lat: 48.0, lon: -120.0 })

    expect(before?.t).toBe(0)
    expect(before?.coordinate.lat).toBeCloseTo(47.0, 9)
    expect(after?.t).toBe(1)
    expect(after?.coordinate.lat).toBeCloseTo(47.1, 9)
  })

  it('picks the nearest segment on a switchback, not the nearest vertex', () => {
    // A hairpin: the route goes north, doubles back south, then north again. A point just
    // east of the middle of the return leg is closest to that leg, even though the corner
    // vertices are what a vertex-only search would find.
    const path: Coordinate[] = [
      { lat: 47.0, lon: -120.0 },
      { lat: 47.1, lon: -120.0 }, // corner
      { lat: 47.1, lon: -120.01 },
      { lat: 47.0, lon: -120.01 }, // and back down
    ]

    const result = nearestPointOnPath(path, { lat: 47.05, lon: -120.0102 })

    expect(result?.segmentIndex).toBe(2)
    expect(result?.t).toBeCloseTo(0.5, 2)
  })

  it('reports which segment was hit, so the caller knows where along the leg it landed', () => {
    const path: Coordinate[] = [
      { lat: 47.0, lon: -120.0 },
      { lat: 47.1, lon: -120.0 },
      { lat: 47.2, lon: -120.0 },
      { lat: 47.3, lon: -120.0 },
    ]

    expect(nearestPointOnPath(path, { lat: 47.25, lon: -120.0 })?.segmentIndex).toBe(2)
  })

  it('survives a repeated point, which providers do emit', () => {
    // A zero-length segment must not produce a division by zero and a NaN position.
    const path: Coordinate[] = [
      { lat: 47.0, lon: -120.0 },
      { lat: 47.0, lon: -120.0 },
      { lat: 47.1, lon: -120.0 },
    ]

    const result = nearestPointOnPath(path, { lat: 47.05, lon: -120.0 })

    expect(result).not.toBeNull()
    expect(Number.isNaN(result?.distanceM ?? NaN)).toBe(false)
    expect(Number.isNaN(result?.t ?? NaN)).toBe(false)
  })

  it('has no answer for a path that is not a line', () => {
    expect(nearestPointOnPath([], SEATTLE)).toBeNull()
    expect(nearestPointOnPath([SEATTLE], SEATTLE)).toBeNull()
  })
})
