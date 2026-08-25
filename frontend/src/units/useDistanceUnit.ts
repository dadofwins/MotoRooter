/**
 * The rider's unit preference.
 *
 * A preference rather than trip state, and the distinction matters here: a trip is
 * world-readable and world-editable by a shareable link, so putting units in the trip
 * document would mean one rider's choice following the link to another. It lives in
 * `localStorage`, belonging to the person and the browser.
 *
 * Storage is treated as something that may refuse. Safari in private browsing throws on
 * `setItem`, and a map that fails to load because a preference could not be saved is a bad
 * trade for a preference.
 */
import { useCallback, useMemo, useState } from 'react'
import type { DistanceUnit } from './format'

const STORAGE_KEY = 'motorooter.distanceUnit'

function isUnit(value: unknown): value is DistanceUnit {
  return value === 'mi' || value === 'km'
}

function readStored(): DistanceUnit | null {
  try {
    const stored: unknown = localStorage.getItem(STORAGE_KEY)
    // Anything could be in there, including a value written by an older build.
    return isUnit(stored) ? stored : null
  } catch {
    return null
  }
}

export interface DistanceUnitPreference {
  readonly unit: DistanceUnit
  /**
   * A function-typed property, not a method.
   *
   * Method shorthand declares "this may be used", so destructuring it off the object — which
   * every caller of a hook does — is flagged as detaching it from its receiver. These objects
   * are records of functions with no `this` to lose, and saying so in the type is what makes
   * `const { setUnit } = useDistanceUnit()` legitimate rather than merely harmless.
   */
  readonly setUnit: (unit: DistanceUnit) => void
}

export function useDistanceUnit(): DistanceUnitPreference {
  // Miles by default: the first riders using this are in Washington.
  const [unit, setUnitState] = useState<DistanceUnit>(() => readStored() ?? 'mi')

  const setUnit = useCallback((next: DistanceUnit) => {
    setUnitState(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Unwritable storage costs the rider a re-choice next visit, and nothing else.
    }
  }, [])

  // Memoised: callers key effects and long-lived objects on hook results, and a fresh object
  // every render is how a drag session came to rebuild itself mid-gesture.
  return useMemo(() => ({ unit, setUnit }), [unit, setUnit])
}
