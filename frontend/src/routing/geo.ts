/**
 * The geometry behind drag-to-reroute.
 *
 * Google's `DirectionsRenderer` only supports dragging routes Google computed, and most of
 * ours come from ORS, so grabbing the line is hand-built. Step one of every drag is turning
 * a grabbed point into a *position along the route*, which is what this module does.
 *
 * Distances are computed on a local planar approximation rather than with full geodesy: at
 * the scale of one route segment the error is negligible, and the alternative buys accuracy
 * that no drag interaction can perceive. What it does not do is treat a degree of longitude
 * as a degree of latitude — those differ by a third at the latitudes this app is used at,
 * and getting it wrong puts the via-point visibly off the line.
 */
import type { Coordinate } from '../api/types'

const EARTH_RADIUS_M = 6_371_008.8
const DEG_TO_RAD = Math.PI / 180

/** Metres per degree of latitude. Constant enough for this purpose. */
const M_PER_DEG_LAT = EARTH_RADIUS_M * DEG_TO_RAD

/** Metres per degree of longitude at a given latitude — shrinks towards the poles. */
function mPerDegLon(latitude: number): number {
  return M_PER_DEG_LAT * Math.cos(latitude * DEG_TO_RAD)
}

/**
 * A point projected into metres, relative to an origin.
 *
 * Every comparison in a single call shares one origin, so the frame cancels out and only
 * relative positions matter.
 */
interface Planar {
  readonly x: number
  readonly y: number
}

function toPlanar(point: Coordinate, origin: Coordinate): Planar {
  return {
    x: (point.lon - origin.lon) * mPerDegLon(origin.lat),
    y: (point.lat - origin.lat) * M_PER_DEG_LAT,
  }
}

export function distanceM(from: Coordinate, to: Coordinate): number {
  const { x, y } = toPlanar(to, from)
  return Math.hypot(x, y)
}

/** Where on a route line a point falls. */
export interface PathPosition {
  /** The point on the line itself — this is where the via-point goes. */
  readonly coordinate: Coordinate
  /** Index of the segment hit: the line from `path[segmentIndex]` to the next point. */
  readonly segmentIndex: number
  /** How far along that segment, 0 at its start and 1 at its end. */
  readonly t: number
  readonly distanceM: number
}

/**
 * The closest position on `path` to `target`.
 *
 * Segment-wise rather than vertex-wise, deliberately: on a switchback the nearest vertex and
 * the nearest piece of road are different, and a twisty-preferring route is mostly
 * switchbacks. `null` when `path` is not a line.
 */
export function nearestPointOnPath(
  path: readonly Coordinate[],
  target: Coordinate,
): PathPosition | null {
  if (path.length < 2) return null

  let best: PathPosition | null = null

  for (let index = 0; index < path.length - 1; index++) {
    const start = path[index]
    const end = path[index + 1]
    if (start === undefined || end === undefined) continue

    // Local frame centred on the target, so its own planar position is the origin.
    const a = toPlanar(start, target)
    const b = toPlanar(end, target)
    const dx = b.x - a.x
    const dy = b.y - a.y
    const lengthSquared = dx * dx + dy * dy

    // A provider repeating a point gives a zero-length segment; clamp rather than divide.
    const t =
      lengthSquared === 0 ? 0 : Math.min(1, Math.max(0, -(a.x * dx + a.y * dy) / lengthSquared))

    const coordinate: Coordinate = {
      lat: start.lat + (end.lat - start.lat) * t,
      lon: start.lon + (end.lon - start.lon) * t,
    }
    const candidate: PathPosition = {
      coordinate,
      segmentIndex: index,
      t,
      distanceM: Math.hypot(a.x + dx * t, a.y + dy * t),
    }
    if (best === null || candidate.distanceM < best.distanceM) best = candidate
  }

  return best
}

/**
 * A single scalar ordering positions along a path, so two of them can be compared.
 *
 * Segment index plus fraction: enough to say which of two grabbed points comes first, which
 * is what decides where a new via-point is inserted among existing ones.
 */
export function alongPath(position: PathPosition): number {
  return position.segmentIndex + position.t
}
