/**
 * The trips this browser has seen.
 *
 * **Not ownership.** Trips have no accounts and are addressed by slug, so anyone with a link
 * has full access — that is the prototype's design rather than a gap this list covers. It is a
 * convenience so a rider can find their way back, and it lives in `localStorage` because it
 * describes a browser, not a trip.
 *
 * The name is stored beside the slug so the list renders without fetching every trip, which
 * against a bucket-backed store would be one request per row.
 *
 * Storage is treated as something that may refuse: Safari in private browsing throws on write,
 * and a list that cannot be saved is not a reason to fail.
 */
import { useCallback, useMemo, useState } from 'react'

const STORAGE_KEY = 'motorooter.visitedTrips'

/** A registry, not an archive: the oldest fall off rather than growing without bound. */
const MAX_REMEMBERED = 20

export interface VisitedTrip {
  readonly slug: string
  readonly name: string
}

function isVisitedTrip(value: unknown): value is VisitedTrip {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return typeof record['slug'] === 'string' && typeof record['name'] === 'string'
}

function readStored(): readonly VisitedTrip[] {
  try {
    const raw: unknown = localStorage.getItem(STORAGE_KEY)
    if (typeof raw !== 'string') return []
    const parsed: unknown = JSON.parse(raw)
    // Anything could be in there, including a shape written by an older build. Entries that
    // are not trips are dropped rather than allowed to reach a render.
    return Array.isArray(parsed) ? parsed.filter(isVisitedTrip) : []
  } catch {
    return []
  }
}

function writeStored(trips: readonly VisitedTrip[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(trips))
  } catch {
    // Unwritable storage costs the rider this list next visit, and nothing else — the links
    // themselves are unaffected.
  }
}

export interface VisitedTrips {
  readonly trips: readonly VisitedTrip[]
  /** Records a trip, or updates its name. Most recent first. */
  readonly remember: (trip: VisitedTrip) => void
  /** Removes it from this list only. The trip itself is untouched and its link still works. */
  readonly forget: (slug: string) => void
}

export function useVisitedTrips(): VisitedTrips {
  const [trips, setTrips] = useState<readonly VisitedTrip[]>(readStored)

  const remember = useCallback((trip: VisitedTrip) => {
    setTrips((previous) => {
      // Most recent first: it is the one being worked on. Renaming updates the row rather
      // than adding a second one for the same document.
      const next = [trip, ...previous.filter((known) => known.slug !== trip.slug)].slice(
        0,
        MAX_REMEMBERED,
      )
      writeStored(next)
      return next
    })
  }, [])

  const forget = useCallback((slug: string) => {
    setTrips((previous) => {
      const next = previous.filter((known) => known.slug !== slug)
      writeStored(next)
      return next
    })
  }, [])

  return useMemo(() => ({ trips, remember, forget }), [trips, remember, forget])
}
