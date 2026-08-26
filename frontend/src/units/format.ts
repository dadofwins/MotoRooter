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

/** Feet per metre. Climb is read in feet by anyone reading distance in miles. */
const FEET_PER_METRE = 3.280_84

/**
 * Rounding for climb, in the *displayed* unit.
 *
 * Nobody plans a day around single metres, and a figure to the metre claims a precision the
 * elevation model behind it does not have — the same reasoning as rounding duration to five
 * minutes. Coarser in feet because the number is three times larger for the same hill.
 */
const CLIMB_ROUNDING = { m: 50, ft: 50 } as const

/** Below this the answer is "flat" rather than a number nobody would act on. */
const FLAT_BELOW_M = 50

/**
 * Total climb, in the unit the rider chose.
 *
 * Deliberately *not* `formatDistance` with a different argument: the toggle means miles versus
 * kilometres for distance and feet versus metres for climb, because that is how riders read the
 * two. Converting climb by 1609 would turn a 3,600 m day into "2.2" — a plausible-looking small
 * number rather than an obvious error, which is the worst kind of unit bug to ship.
 */
export function formatClimb(metres: number, unit: DistanceUnit): string {
  // Judged before conversion, so the threshold means the same hill in both units.
  if (metres < FLAT_BELOW_M) return 'flat'

  const label = unit === 'mi' ? 'ft' : 'm'
  const value = unit === 'mi' ? metres * FEET_PER_METRE : metres
  const step = CLIMB_ROUNDING[label]
  const rounded = Math.round(value / step) * step
  return `${rounded.toLocaleString('en-US')} ${label}`
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
