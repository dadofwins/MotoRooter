import { describe, expect, it } from 'vitest'
import { polylineStyle, toRouteSegments } from './routeLayer'
import type { Coordinate, RouteLeg, Surface, TripLeg } from '../api/types'

/**
 * Turning a trip into drawable segments.
 *
 * Two properties matter more than anything else here. The line must be **continuous** — a
 * rider reading a route at a petrol stop cannot tell a surface change from a routing hole,
 * so consecutive segments have to share their boundary point. And the surface has to be
 * *visible*: preferring dirt is the entire point of the app, and a route that renders
 * gravel identically to tarmac hides the thing the user came for.
 *
 * `SurfaceSpan` indices are inclusive point ranges into `geometry`, per the backend model.
 */

function coords(count: number): Coordinate[] {
  return Array.from({ length: count }, (_, index) => ({ lat: 47 + index / 100, lon: -120 }))
}

function leg(
  geometry: Coordinate[],
  spans: RouteLeg['surface_spans'] = [],
  overrides: Partial<TripLeg> = {},
): TripLeg {
  return {
    intent: 'unpaved',
    start_waypoint_index: 0,
    end_waypoint_index: 1,
    provider_override: null,
    routed: {
      geometry,
      distance_m: 1000,
      duration_s: 60,
      provider: 'fake',
      intent: 'unpaved',
      surface_spans: spans,
      ascent_m: null,
    },
    ...overrides,
  }
}

/** Every point a set of segments would draw, in order, with boundary duplicates removed. */
function drawnPath(segments: ReturnType<typeof toRouteSegments>): Coordinate[] {
  const path: Coordinate[] = []
  for (const segment of segments) {
    for (const point of segment.path) {
      const last = path[path.length - 1]
      if (last?.lat === point.lat && last.lon === point.lon) continue
      path.push(point)
    }
  }
  return path
}

describe('toRouteSegments', () => {
  it('draws the whole geometry as unknown surface when nothing is tagged', () => {
    const geometry = coords(4)

    const segments = toRouteSegments([leg(geometry)])

    expect(segments).toHaveLength(1)
    expect(segments[0]?.surface).toBe<Surface>('unknown')
    expect(segments[0]?.path).toEqual(geometry)
  })

  it('splits on a surface change and shares the boundary point across the seam', () => {
    // Points 0-2 paved, 2-5 unpaved. The shared point 2 must appear in both segments or
    // the line visibly breaks where the tarmac ends.
    const geometry = coords(6)
    const segments = toRouteSegments([
      leg(geometry, [
        { start_index: 0, end_index: 2, surface: 'paved' },
        { start_index: 2, end_index: 5, surface: 'unpaved' },
      ]),
    ])

    expect(segments.map((segment) => segment.surface)).toEqual<Surface[]>(['paved', 'unpaved'])
    expect(segments[0]?.path.at(-1)).toEqual(geometry[2])
    expect(segments[1]?.path[0]).toEqual(geometry[2])
    expect(drawnPath(segments)).toEqual(geometry)
  })

  it('keeps the line continuous across a gap between spans', () => {
    // Nothing says anything about points 2-4. That is missing data, not a missing road.
    const geometry = coords(7)
    const segments = toRouteSegments([
      leg(geometry, [
        { start_index: 0, end_index: 2, surface: 'paved' },
        { start_index: 4, end_index: 6, surface: 'unpaved' },
      ]),
    ])

    expect(segments.map((segment) => segment.surface)).toEqual<Surface[]>([
      'paved',
      'unknown',
      'unpaved',
    ])
    expect(drawnPath(segments)).toEqual(geometry)
  })

  it('merges touching spans of the same surface into one polyline', () => {
    // Providers routinely emit adjacent identical spans. Drawing each one separately
    // multiplies overlays for no visual difference and shows seams at the joins.
    const geometry = coords(7)
    const segments = toRouteSegments([
      leg(geometry, [
        { start_index: 0, end_index: 3, surface: 'unpaved' },
        { start_index: 3, end_index: 6, surface: 'unpaved' },
      ]),
    ])

    expect(segments).toHaveLength(1)
    expect(segments[0]?.path).toEqual(geometry)
  })

  it('clamps spans that run past the end of the geometry', () => {
    const geometry = coords(3)

    const segments = toRouteSegments([
      leg(geometry, [{ start_index: 0, end_index: 99, surface: 'paved' }]),
    ])

    expect(segments[0]?.path).toEqual(geometry)
  })

  it('resolves overlapping spans deterministically, later span wins', () => {
    const geometry = coords(5)

    const segments = toRouteSegments([
      leg(geometry, [
        { start_index: 0, end_index: 4, surface: 'paved' },
        { start_index: 2, end_index: 4, surface: 'unpaved' },
      ]),
    ])

    expect(segments.map((segment) => segment.surface)).toEqual<Surface[]>(['paved', 'unpaved'])
    expect(drawnPath(segments)).toEqual(geometry)
  })

  it('draws nothing for a leg that has not been routed yet', () => {
    // A waypoint pair the user just added has no geometry. Markers still show; no line does.
    expect(toRouteSegments([leg(coords(3), [], { routed: null })])).toEqual([])
  })

  it('draws nothing for geometry too short to be a line', () => {
    expect(toRouteSegments([leg(coords(1))])).toEqual([])
    expect(toRouteSegments([leg([])])).toEqual([])
  })

  it('tags each segment with the leg it came from, so one leg can be re-requested alone', () => {
    // Drag-to-reroute re-requests the affected leg only. Without this the canvas would
    // have to recompute which leg a click landed on.
    const segments = toRouteSegments([leg(coords(3)), leg(coords(3))])

    expect(segments.map((segment) => segment.legIndex)).toEqual([0, 1])
  })

  it('never merges across a leg boundary, even when the surfaces match', () => {
    const first = coords(3)
    const second = [first[2], { lat: 48, lon: -120 }, { lat: 48.1, lon: -120 }] as Coordinate[]

    const segments = toRouteSegments([
      leg(first, [{ start_index: 0, end_index: 2, surface: 'unpaved' }]),
      leg(second, [{ start_index: 0, end_index: 2, surface: 'unpaved' }]),
    ])

    expect(segments).toHaveLength(2)
    expect(segments.map((segment) => segment.legIndex)).toEqual([0, 1])
  })

  it('invents no connector when adjacent legs do not quite meet', () => {
    // Two engines can disagree about the join by a few metres. Drawing a straight line
    // across the discontinuity would hide a real routing defect behind a plausible picture.
    const first = coords(3)
    const second: Coordinate[] = [
      { lat: 49, lon: -121 },
      { lat: 49.1, lon: -121 },
    ]

    const segments = toRouteSegments([leg(first), leg(second)])

    expect(segments).toHaveLength(2)
    expect(segments[0]?.path.at(-1)).toEqual(first[2])
    expect(segments[1]?.path[0]).toEqual(second[0])
  })
})

describe('polylineStyle', () => {
  it('makes unpaved visually distinct from paved, not merely a different colour', () => {
    // Colour alone fails in sunlight, on a phone, and for colour-blind riders. Dirt is
    // dashed; tarmac is solid.
    const paved = polylineStyle('paved')
    const unpaved = polylineStyle('unpaved')

    expect(paved.icons ?? []).toHaveLength(0)
    expect(unpaved.icons ?? []).not.toHaveLength(0)
    // A dashed line is drawn entirely by its icons, so the base stroke is transparent.
    expect(unpaved.strokeOpacity).toBe(0)
    expect(paved.strokeOpacity).toBeGreaterThan(0)
  })

  it('distinguishes unknown surface from both, since absence of data is not dirt', () => {
    const styles = (['paved', 'unpaved', 'unknown'] as const).map((surface) =>
      JSON.stringify(polylineStyle(surface)),
    )

    expect(new Set(styles).size).toBe(3)
  })

  it('keeps the route above the basemap and clickable for later interactions', () => {
    const style = polylineStyle('paved')

    expect(style.clickable).toBe(true)
    expect(style.zIndex).toBeGreaterThan(0)
  })
})
