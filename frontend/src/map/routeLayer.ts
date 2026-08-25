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

  legs.forEach((leg, legIndex) => {
    const geometry = leg.routed?.geometry ?? []
    if (geometry.length < 2) return // not routed yet, or too short to be a line

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
      // Absence of surface data is not evidence of dirt: muted, but still solid.
      return { ...shared, strokeColor: '#6b7280', strokeWeight: 4, strokeOpacity: 0.8 }
  }
}
