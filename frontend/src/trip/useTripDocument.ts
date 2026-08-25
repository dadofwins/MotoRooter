/**
 * The trip document behind the map: reading it, and writing it.
 *
 * Until this existed the app held everything in component state — nothing survived a reload,
 * nothing could be shared, and three MVP items were addressed by a slug that never existed.
 *
 * Deliberately two hooks rather than one. Reading and writing are different concerns, and
 * combining them created a cycle: the content to save is derived from the document that was
 * loaded, so a single hook needed its own output as its input. Split, `useStoredTrip` depends
 * on nothing and `useTripSave` depends on it.
 *
 * Two decisions shape the writing side.
 *
 * **A trip is created without asking.** No name, no dialog — a rider should not fill in a form
 * before putting two points on a map. The slug carries a random suffix rather than being
 * derived from the default name, because two riders who both do nothing but click twice would
 * otherwise collide on one slug and the second would be told their trip already exists.
 *
 * **Edits are saved on a debounce.** A drag commits on every release and each save is a write
 * to a bucket, so the right cadence is "once the rider stops". `CLAUDE.md` reserves throttling
 * for the gesture itself and debouncing for discrete edits like these.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ApiClient } from '../api/client'
import { isApiError } from '../api/errors'
import type { Poi, Trip, TripLeg, Waypoint } from '../api/types'

export type TripReader = Pick<ApiClient, 'getTrip'>
export type TripWriter = Pick<ApiClient, 'createTrip' | 'updateTrip'>

/** What the map currently shows, and what will be written. */
export interface TripContent {
  readonly waypoints: readonly Waypoint[]
  readonly legs: readonly TripLeg[]
  readonly pois: readonly Poi[]
}

export type SaveStatus = 'idle' | 'creating' | 'saving' | 'saved' | 'conflict' | 'failed'

/** Long enough that a burst of drags is one write, short enough to feel saved. */
const SAVE_DEBOUNCE_MS = 900

const URL_PARAM = 'trip'

/**
 * A slug nobody has to think about, and nobody else will take.
 *
 * Not derived from the default name: every rider's first trip would be `new-trip`, and the
 * second one of those is a 409.
 */
function generateSlug(): string {
  return `trip-${Math.random().toString(36).slice(2, 8)}`
}

function readSlugFromUrl(): string | null {
  return new URL(window.location.href).searchParams.get(URL_PARAM)
}

function writeSlugToUrl(slug: string): void {
  const url = new URL(window.location.href)
  url.searchParams.set(URL_PARAM, slug)
  // replaceState, not pushState: creating a trip is not a navigation and should not put a
  // back-button step between the rider and wherever they came from.
  window.history.replaceState(null, '', url)
}

export interface StoredTrip {
  /** The stored document, or null when there is none to read. */
  readonly trip: Trip | null
  readonly error: Error | null
  /** Re-read from storage. Called after a conflict, when what is stored has won. */
  readonly reload: () => void
}

export function useStoredTrip(client: TripReader): StoredTrip {
  const [trip, setTrip] = useState<Trip | null>(null)
  const [error, setError] = useState<Error | null>(null)

  const read = useCallback(
    (slug: string, signal?: AbortSignal) => {
      client.getTrip(slug, signal === undefined ? {} : { signal }).then(
        (fresh) => {
          if (signal?.aborted === true) return
          setTrip(fresh)
        },
        (reason: unknown) => {
          if (signal?.aborted === true) return
          setError(reason instanceof Error ? reason : new Error(String(reason)))
        },
      )
    },
    [client],
  )

  // A trip named in the URL. A shared link is the prototype's entire sharing model.
  useEffect(() => {
    const fromUrl = readSlugFromUrl()
    if (fromUrl === null) return undefined

    const controller = new AbortController()
    read(fromUrl, controller.signal)
    return () => {
      controller.abort()
    }
  }, [read])

  const reload = useCallback(() => {
    // Read from the URL rather than from a captured value: a trip created after mount put its
    // slug there, and that is the one place both halves of this agree on.
    const slug = readSlugFromUrl()
    if (slug !== null) read(slug)
  }, [read])

  return useMemo(() => ({ trip, error, reload }), [trip, error, reload])
}

export interface TripSave {
  readonly slug: string | null
  readonly status: SaveStatus
  readonly error: Error | null
}

export interface TripSaveOptions {
  /** The slug of the stored trip, when one is already known. */
  readonly slug: string | null
  /** Called when the stored document won, so the reader can re-read it. */
  readonly onConflict: () => void
}

function hasContent(content: TripContent): boolean {
  return content.waypoints.length > 0 || content.pois.length > 0
}

export function useTripSave(
  client: TripWriter,
  content: TripContent,
  options: TripSaveOptions,
): TripSave {
  const [created, setCreated] = useState<string | null>(null)
  const [status, setStatus] = useState<SaveStatus>('idle')
  const [error, setError] = useState<Error | null>(null)

  const slug = options.slug ?? created

  // Read inside the effect rather than depended upon, so a new array identity for the same
  // content cannot trigger a write.
  const latest = useRef(content)
  useEffect(() => {
    latest.current = content
  }, [content])

  const onConflict = useRef(options.onConflict)
  useEffect(() => {
    onConflict.current = options.onConflict
  }, [options.onConflict])

  /** Identifies the content, so an unchanged trip is never written twice. */
  const contentKey = useMemo(() => JSON.stringify(content), [content])
  const savedKey = useRef<string | null>(null)

  useEffect(() => {
    if (!hasContent(latest.current)) return undefined
    if (savedKey.current === contentKey) return undefined

    const controller = new AbortController()
    const timer = setTimeout(() => {
      const current = latest.current

      const write = async (): Promise<void> => {
        let target = slug
        if (target === null) {
          // First content: bring a trip into existence, quietly.
          setStatus('creating')
          const trip = await client.createTrip(
            { name: 'New trip', slug: generateSlug() },
            { signal: controller.signal },
          )
          if (controller.signal.aborted) return
          target = trip.slug
          setCreated(trip.slug)
          writeSlugToUrl(trip.slug)
        }

        setStatus('saving')
        await client.updateTrip(
          target,
          {
            waypoints: [...current.waypoints],
            legs: [...current.legs],
            pois: [...current.pois],
          },
          { signal: controller.signal },
        )
        if (controller.signal.aborted) return
        savedKey.current = contentKey
        setStatus('saved')
      }

      write().then(undefined, (reason: unknown) => {
        if (controller.signal.aborted) return
        // A 409 means the backend's own read-merge-retry lost. Merging again here would be
        // guessing on top of an authority that has already tried, so what is stored wins and
        // the rider is told their edit was replaced.
        if (isApiError(reason) && reason.code === 'trip_modified_concurrently') {
          setStatus('conflict')
          onConflict.current()
          return
        }
        setError(reason instanceof Error ? reason : new Error(String(reason)))
        setStatus('failed')
      })
    }, SAVE_DEBOUNCE_MS)

    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [client, contentKey, slug])

  return { slug, status, error }
}
