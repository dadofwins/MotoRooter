/**
 * Routes the waypoints the user has placed.
 *
 * This is the join between the three pieces that existed separately until now: the map
 * reports clicks, the API client routes them, and `routeLayer` draws what comes back. It is
 * deliberately the smallest thing that makes the app do its job — no persistence, no drag,
 * no assistant.
 *
 * Three behaviours matter beyond "it works":
 *
 * - **One request per change of route, not per render.** Every call is metered, and the ORS
 *   free tier is the binding constraint on this app. The effect is keyed on the *contents*
 *   of the waypoint list rather than its identity, so a caller passing a fresh array on
 *   every render costs nothing. Keying on identity is not merely wasteful — it is an
 *   unbounded request loop.
 * - **Stale responses lose.** A user placing points quickly has several requests in flight;
 *   without sequencing, a slow early response lands last and silently reverts their newer
 *   edit. The same failure `DragScheduler` guards against, at a different cadence.
 * - **Superseded requests are aborted**, which stops them spending quota.
 *
 * The whole route is one leg for now. Splitting a trip into legs with their own intents is
 * a product decision that has not been made yet — see `SLICE_INTENT`.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ApiClient } from '../api/client'
import type { LegIntent, TripLeg, Waypoint } from '../api/types'

/** Only the one call is needed, so a test double stays a one-line object. */
export type LegRouter = Pick<ApiClient, 'routeLeg'>

/**
 * The intent every leg is routed with, until the UI can express one.
 *
 * `RouteLegRequest` requires an intent and there is no control for choosing one yet.
 * `twisty_paved` is the honest default for a first route: it is what the app is *for*,
 * where `highway_connector` would quietly make it a worse Google Maps. Whether the rider
 * picks this per leg or the assistant infers it is an open product question.
 */
const SLICE_INTENT: LegIntent = 'twisty_paved'

/**
 * How many routes to keep. Generous for undo and redo across an editing session, and small
 * enough that a long one cannot grow without bound.
 */
const MAX_CACHED_ROUTES = 50

export interface RouteLegState {
  /** Legs ready to draw. Empty until a route has come back. */
  readonly legs: readonly TripLeg[]
  readonly isRouting: boolean
  readonly error: Error | null
}

interface Settled {
  readonly legs: readonly TripLeg[]
  readonly error: Error | null
  /** Which route this result is for, so progress can be derived rather than flagged. */
  readonly key: string
}

const NOTHING: Settled = { legs: [], error: null, key: '' }

export function useRouteLeg(client: LegRouter, waypoints: readonly Waypoint[]): RouteLegState {
  const [settled, setSettled] = useState<Settled>(NOTHING)

  /** Identifies a route by where it goes, not by which array object holds it. */
  const routeKey = useMemo(
    () =>
      waypoints
        .map((waypoint) => `${String(waypoint.coordinate.lat)},${String(waypoint.coordinate.lon)}`)
        .join(';'),
    [waypoints],
  )

  // Read inside the effect rather than depended upon, so the array's identity cannot
  // retrigger a request that its contents did not change.
  const latest = useRef(waypoints)
  useEffect(() => {
    latest.current = waypoints
  }, [waypoints])

  /** Monotonic, so a response can be recognised as superseded before it is applied. */
  const sequenceRef = useRef(0)

  /**
   * Routes already fetched, keyed by where they go.
   *
   * Without it, adding a via point and undoing it costs three requests instead of one, and
   * every undo re-fetches geometry already in hand. Bounded, because an editing session can
   * visit a great many routes.
   *
   * State rather than a ref because it is read while rendering, and a ref read during
   * render is not safe under concurrent rendering. Feeding it back into the effect's
   * dependencies terminates: a cache change re-runs the effect, which finds the route
   * already present and does nothing.
   */
  const [cache, setCache] = useState<ReadonlyMap<string, readonly TripLeg[]>>(() => new Map())

  useEffect(() => {
    const points = latest.current
    if (points.length < 2) return undefined // nothing to route between yet
    if (cache.has(routeKey)) return undefined // already have exactly this route

    // Mounting with two or more waypoints already in place — restored from persistence or
    // a URL — dispatches twice under StrictMode's double-invoke, because the second run
    // starts before the first response has populated the cache. Unreachable while the app
    // always mounts empty; whoever adds restore-on-load should expect it.
    const sequence = ++sequenceRef.current
    const controller = new AbortController()

    client
      .routeLeg(
        { waypoints: points.map((waypoint) => waypoint.coordinate), intent: SLICE_INTENT },
        { signal: controller.signal },
      )
      .then(
        (response) => {
          if (sequence !== sequenceRef.current) return // a newer route already landed
          const legs: readonly TripLeg[] = [
            {
              intent: SLICE_INTENT,
              start_waypoint_index: 0,
              end_waypoint_index: points.length - 1,
              provider_override: null,
              routed: response.leg,
            },
          ]
          setCache((previous) => remember(previous, routeKey, legs))
          setSettled({ legs, error: null, key: routeKey })
        },
        (reason: unknown) => {
          if (sequence !== sequenceRef.current || controller.signal.aborted) return
          // Not recorded as held: a failed route should be retried if it comes back, not
          // remembered as answered.
          setSettled({
            legs: [],
            error: reason instanceof Error ? reason : new Error(String(reason)),
            key: routeKey,
          })
        },
      )

    return () => {
      controller.abort()
    }
  }, [client, routeKey, cache])

  // What is *shown* is derived from what the route currently is, not from the last thing
  // that came back. Storing it instead is how a deleted route stays on the map: the effect
  // has nothing to do when there is nothing to route, so it never clears anything.
  const routable = waypoints.length >= 2
  const cached = routable ? cache.get(routeKey) : undefined
  const failedHere = routable && settled.key === routeKey && settled.error !== null

  return {
    // A cached route shows immediately. Otherwise the previous line stays up while the new
    // one is fetched — blanking the map between edits would be worse — but a route that no
    // longer has two points is simply gone.
    legs: routable ? (cached ?? settled.legs) : [],
    // Only the current route's failure is worth showing. Otherwise removing the waypoints
    // that caused an error leaves an alert on screen that cannot be dismissed.
    error: failedHere ? settled.error : null,
    // Derived, not stored: setting a flag when the request starts would mean a state
    // update inside the effect, which cascades renders.
    isRouting: routable && cached === undefined && !failedHere,
  }
}

/** A copy of the cache with `legs` added, evicting the oldest entry once it is full. */
function remember(
  cache: ReadonlyMap<string, readonly TripLeg[]>,
  key: string,
  legs: readonly TripLeg[],
): ReadonlyMap<string, readonly TripLeg[]> {
  const next = new Map(cache)
  next.set(key, legs)
  if (next.size > MAX_CACHED_ROUTES) {
    const oldest = next.keys().next()
    if (!oldest.done) next.delete(oldest.value)
  }
  return next
}
