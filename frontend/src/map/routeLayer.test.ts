import { describe, expect, it } from 'vitest'
import { polylineStyle, toRouteSegments } from './routeLayer'
import type { Coordinate, RouteLeg, Surface, TripLeg } from '../api/types'
import { routeLeg } from '../api/fixtures'

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
    routed: routeLeg({ geometry: [...geometry], surface_spans: spans }),
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

/**
 * Adjacent segment pairs that do not share a point.
 *
 * `drawnPath` cannot see these: it concatenates every segment's points, so two polylines with
 * a 200 m hole between them read as one continuous list. Each segment becomes its own
 * `google.maps.Polyline`, so what a rider actually sees is the gap.
 */
function gaps(segments: ReturnType<typeof toRouteSegments>): number[] {
  const open: number[] = []
  segments.slice(1).forEach((segment, index) => {
    const before = segments[index]?.path.at(-1)
    const after = segment.path[0]
    if (before === undefined || after === undefined) return
    if (before.lat !== after.lat || before.lon !== after.lon) open.push(index)
  })
  return open
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

  it('closes the joint between two legs whose engines disagree about the shared waypoint', () => {
    // Now that a trip is many legs, every waypoint is a joint between two engines, and they
    // do not return the same coordinate for it: each snaps to its own nearest routable node,
    // sometimes tens of metres apart. Drawn as-is that is a hole in the route at every
    // waypoint, and a rider cannot tell a hole from a routing failure.
    const first = leg(
      [
        { lat: 47.0, lon: -120.0 },
        { lat: 47.1, lon: -120.0 },
      ],
      [],
      { start_waypoint_index: 0, end_waypoint_index: 1 },
    )
    const second = leg(
      [
        // Snapped 200 m from where the first leg ended.
        { lat: 47.102, lon: -120.0 },
        { lat: 47.2, lon: -120.0 },
      ],
      [],
      { start_waypoint_index: 1, end_waypoint_index: 2 },
    )

    const segments = toRouteSegments([first, second])

    // Asserted on the polylines, not on their concatenation. My first version of this test
    // used `drawnPath`, which joins every segment's points end to end and so reported a
    // continuous route across a 200 m hole — it passed before the bridge existed.
    expect(gaps(segments)).toEqual([])
    expect(drawnPath(segments)).toEqual([
      { lat: 47.0, lon: -120.0 },
      { lat: 47.1, lon: -120.0 },
      { lat: 47.102, lon: -120.0 },
      { lat: 47.2, lon: -120.0 },
    ])
  })

  it('does not add a point when the legs already meet', () => {
    const shared = { lat: 47.1, lon: -120.0 }
    const first = leg([{ lat: 47.0, lon: -120.0 }, shared])
    const second = leg([shared, { lat: 47.2, lon: -120.0 }])

    const segments = toRouteSegments([first, second])

    // A duplicated point is harmless to look at and still wrong: it would show up as a
    // zero-length edge in anything measuring the drawn line.
    expect(segments[0]?.path).toHaveLength(2)
  })

  it('leaves a joint open when the next leg has not routed yet', () => {
    const first = leg([
      { lat: 47.0, lon: -120.0 },
      { lat: 47.1, lon: -120.0 },
    ])
    const second = leg(coords(3), [], { routed: null })

    const segments = toRouteSegments([first, second])

    // Nothing to bridge to. Inventing a line to a leg that has no geometry would draw a
    // route the router has not agreed to.
    expect(segments).toHaveLength(1)
    expect(segments[0]?.path).toHaveLength(2)
  })

  it('closes every joint on a trip with several legs and surfaces', () => {
    // The realistic shape: highway, dirt, highway, each from a different engine, each snapping
    // the shared waypoint differently. One gap anywhere reads as a routing failure.
    const legs = [0, 1, 2].map((index) =>
      leg(
        [
          { lat: 47 + index * 0.1, lon: -120 },
          { lat: 47 + index * 0.1 + 0.05, lon: -120 },
          { lat: 47 + index * 0.1 + 0.098, lon: -120 },
        ],
        [{ start_index: 0, end_index: 1, surface: index === 1 ? 'unpaved' : 'paved' }],
        { start_waypoint_index: index, end_waypoint_index: index + 1 },
      ),
    )

    const segments = toRouteSegments(legs)

    expect(segments.length).toBeGreaterThan(3)
    expect(gaps(segments)).toEqual([])
  })

  it('keeps a bridged joint attributable to the leg before it', () => {
    // Segments carry the leg a click should re-request. The joint belongs to one of the two,
    // and giving it to the earlier one keeps the rule "a segment names exactly one leg".
    const first = leg([
      { lat: 47.0, lon: -120.0 },
      { lat: 47.1, lon: -120.0 },
    ])
    const second = leg([
      { lat: 47.102, lon: -120.0 },
      { lat: 47.2, lon: -120.0 },
    ])

    const segments = toRouteSegments([first, second])

    expect(segments[0]?.legIndex).toBe(0)
    expect(segments.at(-1)?.legIndex).toBe(1)
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

  it('invents no connector across a gap too large to be a snapped waypoint', () => {
    // The other half of the joint rule. A few hundred metres is two engines snapping the same
    // waypoint to different nodes, and closing it is honest. This gap is 200 km: the legs
    // genuinely do not connect, and drawing a straight line across it would hide a real
    // routing defect behind a plausible picture.
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

  it('draws unknown as present as the rest of the route, differing only in hue', () => {
    // It was thinner and fainter as well as grey, over a basemap whose own roads are grey
    // and white — so on a real route the 29% that is unsurveyed dissolved into the map and
    // read as "the route is not there" rather than "we do not know about this bit". A rider
    // planning fuel and tyres needs to see that third of the route, not look past it.
    const paved = polylineStyle('paved')
    const unknown = polylineStyle('unknown')
    const unpaved = polylineStyle('unpaved')

    expect(unknown.strokeWeight).toBe(paved.strokeWeight)
    expect(unknown.strokeOpacity).toBe(paved.strokeOpacity)
    // Unpaved's own stroke is transparent because its dashes are drawn by icons, so its
    // presence is carried by those instead.
    expect(unpaved.icons?.[0]?.icon?.strokeWeight).toBe(paved.strokeWeight)
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
