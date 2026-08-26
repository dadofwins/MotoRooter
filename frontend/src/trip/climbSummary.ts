/**
 * How much a trip climbs, and how much of it nobody measured.
 *
 * Climb was suppressed for months on a real discrepancy: ORS reported 6,400-8,800 m for a section
 * whose published figure is 3,188 m. That is now explained — `cycling-mountain` returned an exact
 * 0 where its elevation lookup failed, and twelve such points in 2,763 accounted for 3,124 m,
 * because each plunge to sea level and back adds twice the local elevation to a cumulative sum.
 * Nothing was ever computed wrong; a bad input was being reported faithfully. With the sentinel
 * filtered in the adapter the figure is worth showing, and worth showing because a 3,600 m day and
 * a 1,500 m day are different rides over the same distance.
 *
 * **The catch is that only some engines measure it.** Google reports no elevation at all, so on a
 * mixed trip — highway, dirt, highway, the shape of Tim's own test route — climb exists for 40 km
 * of 269. Presenting that as the trip's climb would understate it about sixfold.
 *
 * So this returns the unmeasured distance beside the figure, and the caller shows both. It is the
 * same rule as `unpaved_fraction` not counting `Surface.UNKNOWN`: unknown stays unknown rather
 * than being folded into a number that looks complete. Under-reporting is the safe direction for
 * dirt because a rider who finds more dirt than promised has a better day; for climb the honest
 * direction is to say how much of the route the figure covers, because a rider planning a day
 * around 1,200 m who rides 3,000 m has a much worse one.
 */
import type { TripLeg } from '../api/types'

export interface ClimbSummary {
  /** Metres of ascent over the legs that measured it, or `null` when none did. */
  readonly ascentM: number | null
  /** Distance the figure covers. */
  readonly measuredDistanceM: number
  /** Distance routed by an engine that reports no elevation. Not flat — unknown. */
  readonly unmeasuredDistanceM: number
  /** Share of the routed distance the figure covers, 0 when nothing is routed. */
  readonly measuredFraction: number
}

export function climbSummary(legs: readonly TripLeg[]): ClimbSummary {
  let ascentM: number | null = null
  let measuredDistanceM = 0
  let unmeasuredDistanceM = 0

  for (const leg of legs) {
    const routed = leg.routed
    // No geometry means no distance and no elevation: it is not unmeasured road, it is road that
    // has not been routed yet.
    if (routed === null || routed === undefined) continue

    const ascent = routed.ascent_m ?? null
    if (ascent === null) {
      unmeasuredDistanceM += routed.distance_m
      continue
    }
    // Zero is a real answer on flat ground, now that the sentinel is filtered upstream. Treating
    // it as missing would throw away the distinction the field carries honestly.
    ascentM = (ascentM ?? 0) + ascent
    measuredDistanceM += routed.distance_m
  }

  const total = measuredDistanceM + unmeasuredDistanceM
  return {
    ascentM,
    measuredDistanceM,
    unmeasuredDistanceM,
    measuredFraction: total === 0 ? 0 : measuredDistanceM / total,
  }
}
