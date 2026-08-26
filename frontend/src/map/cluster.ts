/**
 * Grouping pins that would land on top of each other.
 *
 * Discovery returns places along a corridor, and a corridor is narrow. Several campgrounds off
 * the same forest road are hundreds of metres apart, which at the zoom that shows a whole day's
 * ride is a handful of pixels: the pins cover each other, the one on top is whichever was drawn
 * last, and the rider cannot see how many places are there or click the ones underneath.
 *
 * **The decision is made in screen space, not in metres.** The question is whether two pins
 * overlap, and that depends on the zoom — the same two places are one blob at zoom 8 and two
 * clearly separate pins at zoom 14. A metre threshold would answer a different question and be
 * wrong at every zoom but one.
 */
import type { Coordinate, Poi } from '../api/types'

/**
 * How close two pins have to be before they are treated as one.
 *
 * A pin is 24px across with a 2px border, so two of them touch at 28px between centres and
 * genuinely obscure each other below that. Slightly wider than touching, because two pins with a
 * two-pixel gap are already unreadable and unclickable as separate targets.
 */
export const CLUSTER_RADIUS_PX = 34

/** The tile size the Maps API projects into. Not a tunable: it is the API's own unit. */
const WORLD_PX = 256

export interface PixelPoint {
  readonly x: number
  readonly y: number
}

/**
 * Where a coordinate falls in map pixels at a given zoom.
 *
 * Web Mercator, the projection the Maps API uses, so a pixel here is the pixel the rider sees.
 * Latitude is clamped at the poles, where the projection runs to infinity — a route there is not
 * a real case, but an infinity propagating into a distance comparison silently puts everything
 * in one cluster.
 */
export function pixelsAt(coordinate: Coordinate, zoom: number): PixelPoint {
  const scale = WORLD_PX * 2 ** zoom
  const lat = Math.min(85.05112878, Math.max(-85.05112878, coordinate.lat))
  const sin = Math.sin((lat * Math.PI) / 180)
  return {
    x: ((coordinate.lon + 180) / 360) * scale,
    // Negated because y grows southward while latitude grows northward.
    y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale,
  }
}

export interface PoiCluster {
  /**
   * Identity for the marker layer, derived from the members rather than from a position in a
   * list. Keyed on an index, inserting one place at the front would rebuild every marker after
   * it — the churn that turns a long editing session into treacle.
   */
  readonly key: string
  /** Where to draw it: the place itself when there is one, the middle of the group otherwise. */
  readonly coordinate: Coordinate
  readonly members: readonly Poi[]
}

export interface ClusterOptions {
  readonly zoom: number
  readonly radiusPx?: number
}

/**
 * Group places whose pins would overlap at this zoom.
 *
 * Greedy around an anchor rather than transitive merging: each unassigned place in turn takes
 * everything still unassigned within a radius of *it*. A string of places along a road, each a
 * radius from the next, is a road and not a pile — transitive merging would collapse the whole
 * line into one pin and hide the shape of the ride.
 *
 * Deterministic by construction, because the map redraws constantly and pins that reshuffle
 * between renders are worse than pins that overlap.
 */
export function clusterPois(
  pois: readonly Poi[],
  { zoom, radiusPx = CLUSTER_RADIUS_PX }: ClusterOptions,
): readonly PoiCluster[] {
  const points = pois.map((poi) => pixelsAt(poi.coordinate, zoom))
  const taken = new Set<number>()
  const clusters: PoiCluster[] = []

  pois.forEach((anchor, index) => {
    if (taken.has(index)) return
    taken.add(index)
    const members = [anchor]

    const from = points[index]
    pois.forEach((candidate, other) => {
      if (other <= index || taken.has(other)) return
      const to = points[other]
      if (from === undefined || to === undefined) return
      if (Math.hypot(to.x - from.x, to.y - from.y) > radiusPx) return
      taken.add(other)
      members.push(candidate)
    })

    clusters.push({
      // Sorted, so the same grouping reached from a different input order is the same cluster.
      key: [...members.map((member) => member.id)].sort().join('+'),
      coordinate: centreOf(members),
      members,
    })
  })

  return clusters
}

/** The middle of a group — and a lone place's own coordinate, exactly, not an average of one. */
function centreOf(members: readonly Poi[]): Coordinate {
  const only = members[0]
  if (only === undefined) throw new RangeError('a cluster with no members')
  if (members.length === 1) return only.coordinate
  return {
    lat: members.reduce((total, each) => total + each.coordinate.lat, 0) / members.length,
    lon: members.reduce((total, each) => total + each.coordinate.lon, 0) / members.length,
  }
}
