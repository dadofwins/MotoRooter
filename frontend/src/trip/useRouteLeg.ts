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

  useEffect(() => {
    const points = latest.current
    if (points.length < 2) return undefined // nothing to route between yet

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
          setSettled({
            legs: [
              {
                intent: SLICE_INTENT,
                start_waypoint_index: 0,
                end_waypoint_index: points.length - 1,
                provider_override: null,
                routed: response.leg,
              },
            ],
            error: null,
            key: routeKey,
          })
        },
        (reason: unknown) => {
          if (sequence !== sequenceRef.current || controller.signal.aborted) return
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
  }, [client, routeKey])

  return {
    legs: settled.legs,
    error: settled.error,
    // Derived, not stored: setting a flag when the request starts would mean a state
    // update inside the effect, which cascades renders.
    isRouting: waypoints.length >= 2 && settled.key !== routeKey,
  }
}
