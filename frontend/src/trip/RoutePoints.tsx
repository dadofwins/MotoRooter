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
 * It is also where the *segments* live. A route is points and the legs between them, and a leg
 * carries its own routing mode — so the outline is where a rider changes one, which is the mouse
 * equivalent of the assistant's `set_leg_intent`.
 *
 * Deliberately not a reorder control. Dragging rows to reorder a route is a different
 * interaction with its own failure modes, and the map is the honest place to express order.
 */
import { LegModePicker } from './LegModePicker'
import type { DistanceUnit } from '../units/format'
import type { LegIntent, TripLeg, Waypoint } from '../api/types'

export interface RoutePointsProps {
  readonly waypoints: readonly Waypoint[]
  /** Remove the point at this index. The caller re-shapes the legs around it. */
  readonly onRemove: (index: number) => void
  /**
   * The segments between the points. Omitted, the outline is points only — which is what it was
   * before modes were choosable, and still right for a trip with nothing routed.
   */
  readonly legs?: readonly TripLeg[]
  /** From `GET /api/routing/capabilities`, never a list kept here. */
  readonly reportsSurface?: (intent: LegIntent) => boolean | null
  readonly reportsTrustworthyDuration?: (intent: LegIntent) => boolean | null
  readonly reportsElevation?: (intent: LegIntent) => boolean | null
  /**
   * Riding time per leg, parallel to `legs`, from `useRouteLegs`.
   *
   * Distance and climb come off each leg's own geometry, but the estimate is computed per request
   * and does not live on `TripLeg` — so it arrives beside the legs rather than on them.
   */
  readonly legDurationsS?: readonly (number | null)[]
  readonly unit?: DistanceUnit
  readonly onIntentChange?: (legIndex: number, intent: LegIntent) => void
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

export function RoutePoints({
  waypoints,
  onRemove,
  legs,
  reportsSurface,
  reportsTrustworthyDuration,
  reportsElevation,
  legDurationsS,
  unit = 'mi',
  onIntentChange,
}: RoutePointsProps): React.JSX.Element | null {
  // Nothing rather than an empty list: a heading over no rows reads as broken rather than
  // as a route not started.
  if (waypoints.length === 0) return null

  /**
   * Whether saying "placed by you" distinguishes anything.
   *
   * Every point a rider clicks is pinned, so on a route nobody has replanned the label appears on
   * every row and tells them nothing — five identical annotations, which is how it looked once the
   * rail was rendered and examined. It earns its place only when the list is mixed, which is
   * exactly when a replan has added or moved points the rider did not place.
   */
  /**
   * A key that is both unique and stable, which the obvious two choices are not.
   *
   * The index remounts every row below a removal and costs a keyboard user the focus they were
   * holding. The bare coordinate collides on a round trip — the first real trip anyone planned
   * was "starting in Woodinville and coming back", so the same place appears twice and React
   * associates a row with the wrong node.
   *
   * Numbering the repeats gives both: removing some other point leaves every key untouched, and
   * two stops at the same place stay distinct.
   */
  const seenAt = new Map<string, number>()
  const keys = waypoints.map((waypoint) => {
    const place = `${String(waypoint.coordinate.lat)},${String(waypoint.coordinate.lon)}`
    const repeat = seenAt.get(place) ?? 0
    seenAt.set(place, repeat + 1)
    return `${place}#${String(repeat)}`
  })

  const mixedProvenance =
    waypoints.some((waypoint) => waypoint.pinned) &&
    waypoints.some((waypoint) => !waypoint.pinned)

  return (
    <section className="points" aria-label="Route points">
      <h2 className="points__title">Route</h2>
      <ol className="points__list">
        {waypoints.map((waypoint, index) => {
          const label = describe(waypoint)
          return (
            // Keyed on where the point is, not on its position in the list. Keying on the index
            // remounted every row below a removal, which cost a keyboard user the focus they
            // were holding mid-list.
            <li key={keys[index]} className="points__row">
              <span className="points__index" aria-hidden="true">
                {index + 1}
              </span>
              <span className="points__label">
                {label}
                {mixedProvenance && waypoint.pinned && (
                  // A replan may move or drop an unpinned point and must leave a pinned one
                  // alone, so this is worth seeing before pressing Replan rather than after —
                  // but only where there is something to tell it apart from.
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

      {/* The segments, after the points they join. A leg can span more than two waypoints once
          a drag has put a via inside it, so each picker names its leg's own endpoints rather
          than the rows it happens to sit between. */}
      {legs !== undefined &&
        reportsSurface !== undefined &&
        onIntentChange !== undefined &&
        legs.length > 0 && (
          <div className="points__segments">
            {legs.map((leg, legIndex) => {
              const from = waypoints[leg.start_waypoint_index]
              const to = waypoints[leg.end_waypoint_index]
              if (from === undefined || to === undefined) return null
              return (
                <LegModePicker
                  key={`${String(leg.start_waypoint_index)}-${String(leg.end_waypoint_index)}`}
                  legIndex={legIndex}
                  intent={leg.intent}
                  from={describe(from)}
                  to={describe(to)}
                  reportsSurface={reportsSurface}
                  reportsTrustworthyDuration={reportsTrustworthyDuration ?? (() => null)}
                  reportsElevation={reportsElevation ?? (() => null)}
                  distanceM={leg.routed?.distance_m ?? null}
                  durationS={legDurationsS?.[legIndex] ?? null}
                  ascentM={leg.routed?.ascent_m ?? null}
                  unit={unit}
                  onChange={onIntentChange}
                />
              )
            })}
          </div>
        )}
    </section>
  )
}
