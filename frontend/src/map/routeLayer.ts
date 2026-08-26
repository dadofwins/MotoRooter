/**
 * Trip legs to drawable, surface-aware polyline segments.
 *
 * Google's `DirectionsRenderer` can only draw routes Google computed, and most of ours come
 * from ORS, so the route is our own polylines. That makes this the layer where two things
 * have to be got right:
 *
 * - **Continuity.** Consecutive segments share their boundary point. A rider cannot tell a
 *   surface change from a hole in the routing, so the line must never appear to break.
 * - **Legibility of surface.** Preferring dirt is the point of the app. Gravel that renders
 *   like tarmac hides the reason the route was chosen.
 *
 * Kept pure and free of any `google.maps` dependency so the splitting rules — which is
 * where the off-by-one bugs live — are testable without the API.
 */
import { distanceM } from '../routing/geo'
import type { Coordinate, Surface, TripLeg } from '../api/types'

/** One run of geometry sharing a surface, ready to become a single polyline. */
export interface RouteSegment {
  readonly path: readonly Coordinate[]
  readonly surface: Surface
  /** Index into the trip's legs. Drag-to-reroute re-requests one leg, never the route. */
  readonly legIndex: number
}

const DEFAULT_SURFACE: Surface = 'unknown'

/**
 * How far apart two legs' shared waypoint may be and still count as the same place.
 *
 * Each engine snaps a waypoint to its own nearest routable node, so a joint is never exact —
 * tens of metres in a town, a few hundred where the nearest mapped road is far away. Below
 * this the hole is an artefact and closing it is honest.
 *
 * Above it, the hole is *information*: two legs that genuinely do not connect mean the route
 * is broken, and drawing a straight line across kilometres of nothing would hide a real defect
 * behind a plausible picture. This is why the threshold exists rather than bridging every
 * joint — the two cases look identical in the data and only the distance separates them.
 *
 * Measured on the live stack, Woodinville → Cashmere → Blewett Pass → Ellensburg with one
 * intent per leg: **0.5 m** at the google→ors joint and **11.3 m** at ors→google. So real
 * disagreement is far smaller than this ceiling, which is deliberate — those two joints are on
 * mapped road, and a waypoint dropped in the middle of a clear-cut snaps much further. Retune
 * it if a real gap is ever seen inside it, not on the strength of two happy samples.
 */
const JOINT_BRIDGE_M = 500

/**
 * Surface of each *edge* of a leg's geometry.
 *
 * `SurfaceSpan` indices are inclusive point ranges, so a span covers the edges between its
 * points — `[2, 5]` owns edges 2→3, 3→4 and 4→5. Modelling edges rather than points is
 * what makes the boundary between two spans unambiguous: the shared point belongs to both
 * segments, and the seam closes.
 */
function edgeSurfaces(leg: TripLeg): Surface[] {
  const geometry = leg.routed?.geometry ?? []
  const surfaces: Surface[] = Array.from({ length: Math.max(geometry.length - 1, 0) }, () => DEFAULT_SURFACE)

  // Later spans win where they overlap, so the result is deterministic whatever order the
  // provider emitted them in.
  for (const span of leg.routed?.surface_spans ?? []) {
    const first = Math.max(span.start_index, 0)
    const last = Math.min(span.end_index, surfaces.length)
    for (let edge = first; edge < last; edge++) surfaces[edge] = span.surface
  }
  return surfaces
}

export function toRouteSegments(legs: readonly TripLeg[]): RouteSegment[] {
  const segments: RouteSegment[] = []

  /**
   * Closes the joint between the previous leg and this one.
   *
   * Now that a trip is many legs, every waypoint is a boundary between two engines, and they
   * do not agree on its coordinate: each snaps to its own nearest routable node, sometimes
   * tens of metres away. Each segment becomes its own `google.maps.Polyline`, so left alone
   * that is a visible hole at every waypoint — and a rider cannot tell a hole from a routing
   * failure.
   *
   * The bridge is one point appended to the *previous* segment, which keeps the invariant
   * this module is built on: consecutive segments share their boundary point, and every
   * segment still names exactly one leg for a click to re-request.
   */
  const bridgeTo = (start: Coordinate): void => {
    const previous = segments.at(-1)
    const end = previous?.path.at(-1)
    if (previous === undefined || end === undefined) return
    if (end.lat === start.lat && end.lon === start.lon) return
    // A gap this size is not a snapping artefact, and a rider is entitled to see it.
    if (distanceM(end, start) > JOINT_BRIDGE_M) return
    segments[segments.length - 1] = { ...previous, path: [...previous.path, start] }
  }

  legs.forEach((leg, legIndex) => {
    const geometry = leg.routed?.geometry ?? []
    if (geometry.length < 2) return // not routed yet, or too short to be a line

    const start = geometry[0]
    if (start !== undefined) bridgeTo(start)

    const surfaces = edgeSurfaces(leg)
    let runStart = 0
    for (let edge = 1; edge <= surfaces.length; edge++) {
      // A run ends at the last edge or where the surface changes. Never merge across legs:
      // segments stay attributable to the leg that must be re-requested.
      if (edge < surfaces.length && surfaces[edge] === surfaces[runStart]) continue
      segments.push({
        // `edge + 1` because a run of edges [runStart, edge) spans points runStart..edge.
        path: geometry.slice(runStart, edge + 1),
        surface: surfaces[runStart] ?? DEFAULT_SURFACE,
        legIndex,
      })
      runStart = edge
    }
  })

  return segments
}

/**
 * How each surface is drawn.
 *
 * Dirt is dashed rather than merely a different colour: colour alone is unreadable in
 * direct sun, on a phone, and for a colour-blind rider, and this is the distinction the
 * whole app exists to show. Colours are deliberately saturated enough to hold up over both
 * the light and dark basemaps.
 */
export function polylineStyle(surface: Surface): google.maps.PolylineOptions {
  const shared = { clickable: true, zIndex: 10 } as const

  switch (surface) {
    case 'unpaved':
      return {
        ...shared,
        strokeColor: '#c2571a',
        strokeWeight: 5,
        // A dashed line is drawn entirely by repeated icons, so the stroke itself is hidden.
        strokeOpacity: 0,
        icons: [
          {
            icon: {
              path: 'M 0,-1 0,1',
              strokeColor: '#c2571a',
              strokeOpacity: 1,
              strokeWeight: 5,
              scale: 1,
            },
            offset: '0',
            repeat: '11px',
          },
        ],
      }
    case 'paved':
      return { ...shared, strokeColor: '#1f6feb', strokeWeight: 5, strokeOpacity: 0.95 }
    case 'unknown':
    default:
      // Absence of surface data is not evidence of dirt — but it is not a reason to recede
      // either. Grey at the same weight and opacity as the rest: three equally present
      // states differing in what is *known*, not in how much they matter. Thinner and
      // fainter, over a basemap whose own roads are grey and white, made the unsurveyed
      // third of a real route dissolve into the map and read as missing.
      return { ...shared, strokeColor: '#6b7280', strokeWeight: 5, strokeOpacity: 0.95 }
  }
}
