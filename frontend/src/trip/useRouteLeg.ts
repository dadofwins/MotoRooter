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
import { isLegStale } from '../routing/tripEdits'
import type { LegIntent, TripLeg, Waypoint } from '../api/types'

/** Only the one call is needed, so a test double stays a one-line object. */
export type LegRouter = Pick<ApiClient, 'routeLeg'>

/**
 * Intents that route through an engine able to report surface.
 *
 * Not a stylistic preference — a correctness constraint. An intent that resolves to an
 * engine with no surface data returns zero spans, every metre renders as `unknown` grey,
 * and the one distinction this app exists to draw silently disappears. Measured on
 * Woodinville → Cashmere → Ellensburg: `twisty_paved` resolves to Google and reports 0
 * spans over 270 km; `unpaved` resolves to ORS and reports 139.
 *
 * Kept as *intents* rather than provider names deliberately: which engine serves an intent
 * is the backend's policy table to decide, and naming one here would hardcode today's
 * answer to a question that is configuration.
 */
export const SURFACE_REPORTING_INTENTS = ['unpaved', 'technical_offroad'] as const

/**
 * The intent every leg is routed with, until the UI can express one.
 *
 * `RouteLegRequest` requires an intent and there is no control for choosing one yet. Dirt
 * is the point of an adventure motorcycle planner, and — see above — it is also the only
 * way the rider sees any surface information at all. Whether the rider picks this per leg
 * or the assistant infers it is still an open product question.
 */
const SLICE_INTENT: LegIntent = 'unpaved'

/**
 * How many routes to keep. Generous for undo and redo across an editing session, and small
 * enough that a long one cannot grow without bound.
 */
const MAX_CACHED_ROUTES = 50

export interface RouteLegState {
  /** Legs ready to draw. Empty until a route has come back. */
  readonly legs: readonly TripLeg[]
  /**
   * Riding time for the route, in seconds, or `null` when nothing has estimated it.
   *
   * From `RouteLegResponse.estimated_duration_s`, derived server-side from distance and
   * surface so the speed table has one home. Never `leg.duration_s`, which on dirt is a
   * bicycle time — the backend measured 8 hours for 133 km. Null rather than zero, because
   * zero is a duration and would read as "under 5m" for a route nobody has estimated.
   */
  readonly estimatedDurationS: number | null
  readonly isRouting: boolean
  readonly error: Error | null
}

interface Settled {
  readonly legs: readonly TripLeg[]
  readonly estimatedDurationS: number | null
  readonly error: Error | null
  /** Which route this result is for, so progress can be derived rather than flagged. */
  readonly key: string
}

const NOTHING: Settled = { legs: [], estimatedDurationS: null, error: null, key: '' }

export function useRouteLeg(
  client: LegRouter,
  waypoints: readonly Waypoint[],
  /**
   * Legs the caller already holds — from a drag, which routes on release itself.
   *
   * Used instead of re-requesting when they still match the waypoints. Freshness comes from
   * `RouteLeg.routed_from` rather than from who called last, so this cannot be fooled by
   * ordering. Without it a drag costs two requests: one to place the via-point and one for
   * the hook to discover the same route again.
   */
  known: readonly TripLeg[] | null = null,
): RouteLegState {
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

  const knownIsFresh =
    known !== null && known.length > 0 && !known.some((leg) => isLegStale(waypoints, leg))

  useEffect(() => {
    const points = latest.current
    if (points.length < 2) return undefined // nothing to route between yet
    if (knownIsFresh) return undefined // the caller routed it; asking again buys nothing
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
          setSettled({
            legs,
            estimatedDurationS: response.estimated_duration_s,
            error: null,
            key: routeKey,
          })
        },
        (reason: unknown) => {
          if (sequence !== sequenceRef.current || controller.signal.aborted) return
          // Not recorded as held: a failed route should be retried if it comes back, not
          // remembered as answered.
          setSettled({
            legs: [],
            estimatedDurationS: null,
            error: reason instanceof Error ? reason : new Error(String(reason)),
            key: routeKey,
          })
        },
      )

    return () => {
      controller.abort()
    }
  }, [client, routeKey, cache, knownIsFresh])

  // What is *shown* is derived from what the route currently is, not from the last thing
  // that came back. Storing it instead is how a deleted route stays on the map: the effect
  // has nothing to do when there is nothing to route, so it never clears anything.
  const routable = waypoints.length >= 2
  const cached = routable ? cache.get(routeKey) : undefined
  const failedHere = routable && settled.key === routeKey && settled.error !== null

  return {
    // Freshly dragged legs win, then a cached route, then the previous line while the new
    // one is fetched — blanking the map between edits would be worse. A route that no
    // longer has two points is simply gone.
    legs: routable ? ((knownIsFresh ? known : null) ?? cached ?? settled.legs) : [],
    // Tied to the route it was estimated for: a stale time beside a changed route is worse
    // than none, and this is the number a rider plans a day around.
    estimatedDurationS: routable && settled.key === routeKey ? settled.estimatedDurationS : null,
    // Only the current route's failure is worth showing. Otherwise removing the waypoints
    // that caused an error leaves an alert on screen that cannot be dismissed.
    error: failedHere ? settled.error : null,
    // Derived, not stored: setting a flag when the request starts would mean a state
    // update inside the effect, which cascades renders.
    isRouting: routable && !knownIsFresh && cached === undefined && !failedHere,
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
