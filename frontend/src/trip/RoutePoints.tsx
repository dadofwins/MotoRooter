/**
 * The route as an ordered list of points, each removable.
 *
 * This exists because of the rule that governs everything: **chat is an accelerator, never a
 * requirement.** The mouse could only ever remove the *last* point, so taking a via-point out
 * of the middle of a route was something only the assistant would be able to do once it has
 * `remove_waypoint` — which is the exact inversion the rule forbids.
 *
 * A list, not only right-click-the-pin. Right-click is the fast path and it is undiscoverable;
 * a list is visible, keyboard-reachable, and it is where the segments *between* the points will
 * live when each gets its own routing mode.
 *
 * Deliberately not a reorder control. Dragging rows to reorder a route is a different
 * interaction with its own failure modes, and the map is the honest place to express order.
 */
import type { Waypoint } from '../api/types'

export interface RoutePointsProps {
  readonly waypoints: readonly Waypoint[]
  /** Remove the point at this index. The caller re-shapes the legs around it. */
  readonly onRemove: (index: number) => void
}

/**
 * Four decimals: about 11 m, which is finer than a rider can distinguish on a map and coarse
 * enough to read. Only shown for a point nobody has named — which today is every point placed
 * by clicking, because there is no forward geocoding yet.
 */
function describe(waypoint: Waypoint): string {
  if (waypoint.name !== null && waypoint.name !== undefined && waypoint.name !== '') {
    return waypoint.name
  }
  return `${waypoint.coordinate.lat.toFixed(4)}, ${waypoint.coordinate.lon.toFixed(4)}`
}

export function RoutePoints({ waypoints, onRemove }: RoutePointsProps): React.JSX.Element | null {
  // Nothing rather than an empty list: a heading over no rows reads as broken rather than
  // as a route not started.
  if (waypoints.length === 0) return null

  return (
    <section className="points" aria-label="Route points">
      <h2 className="points__title">Points</h2>
      <ol className="points__list">
        {waypoints.map((waypoint, index) => {
          const label = describe(waypoint)
          return (
            <li key={`${String(index)}-${label}`} className="points__row">
              <span className="points__index" aria-hidden="true">
                {index + 1}
              </span>
              <span className="points__label">
                {label}
                {waypoint.pinned && (
                  // A replan may move or drop an unpinned point and must leave a pinned one
                  // alone, so this is worth seeing before pressing Replan rather than after.
                  <span className="points__pinned"> · placed by you</span>
                )}
              </span>
              <button
                type="button"
                className="points__remove"
                // Named, because three bare crosses are unusable with a screen reader and
                // ambiguous with a mouse once the list is longer than the rail.
                aria-label={
                  waypoint.name !== null && waypoint.name !== undefined && waypoint.name !== ''
                    ? `Remove ${waypoint.name}`
                    : `Remove point ${String(index + 1)}`
                }
                onClick={() => onRemove(index)}
              >
                ×
              </button>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
