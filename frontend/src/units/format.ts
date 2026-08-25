/**
 * How distances and times are written for a rider.
 *
 * One formatter, used everywhere a distance appears — the route summary, the surface
 * breakdown, and anything added later. Two formatters that agree today drift apart within a
 * week, and a route reading 305 km beside a breakdown adding to 189 mi is worse than either
 * figure alone.
 */

/** A rider's preferred unit. Miles by default: this app's first users are in Washington. */
export type DistanceUnit = 'mi' | 'km'

const METRES_PER = { km: 1000, mi: 1609.344 } as const

/**
 * Below this many of *the unit being shown*, keep a tenth.
 *
 * Deliberately expressed in the displayed unit rather than in metres. A metres threshold
 * would land at 10 km but at 6.2 mi, so the same route would gain or lose a decimal purely
 * by switching preference — the reader's sense of "small enough to need a tenth" does not
 * work that way.
 */
const TENTHS_BELOW = 10

export function formatDistance(metres: number, unit: DistanceUnit): string {
  const value = metres / METRES_PER[unit]
  return `${value.toFixed(value < TENTHS_BELOW ? 1 : 0)} ${unit}`
}

/** Coarse on purpose — see `formatDuration`. */
const ROUNDING_MINUTES = 5

/**
 * Riding time, written as the estimate it is.
 *
 * The figure behind this comes from a speed table the backend is explicit about: reasoned
 * guesses rather than measurements, with its own note that nobody maintains 80 km/h for six
 * hours. So both the wording and the rounding hedge. "4:19" claims a precision the number has
 * not earned; "about 4h 20m" says the same thing honestly, and a rider planning a day cannot
 * use the difference anyway.
 *
 * Note this is never the provider's `duration_s`, which for dirt comes from a bicycle profile
 * and reads roughly twice as long as a motorcycle would take.
 */
export function formatDuration(seconds: number): string {
  const totalMinutes = Math.round(seconds / 60 / ROUNDING_MINUTES) * ROUNDING_MINUTES
  // Anything this short is inside the noise of any speed table; a number would be invention.
  if (totalMinutes < ROUNDING_MINUTES) return `under ${String(ROUNDING_MINUTES)}m`

  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  if (hours === 0) return `about ${String(minutes)}m`
  if (minutes === 0) return `about ${String(hours)}h`
  return `about ${String(hours)}h ${String(minutes)}m`
}
