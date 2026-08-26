/**
 * The provider capabilities the drag interaction is throttled by.
 *
 * The interval is never a constant in the frontend. A cheap engine can refresh near-live
 * while a metered one must hold off, and which engine serves an intent is the backend's
 * policy table to decide — so an interval hardcoded here would diverge silently from
 * whatever is actually serving the leg. On ORS's free tier, diverging in the wrong
 * direction exhausts the day's quota in a single session.
 *
 * Everything unknown is preview-only. Not knowing the cadence is not a licence to pick one.
 */
import { useEffect, useMemo, useState } from 'react'
import type { ApiClient } from '../api/client'
import type { LegIntent, RoutingCapabilitiesResponse } from '../api/types'

export type CapabilitiesReader = Pick<ApiClient, 'routingCapabilities'>

export interface RoutingCapabilitiesState {
  readonly capabilities: RoutingCapabilitiesResponse | null
  readonly isLoaded: boolean
  readonly error: Error | null
  /**
   * Milliseconds between live re-routes while dragging this intent, or `null` for
   * preview-only: rubber-band locally and route only on release.
   *
   * A function-typed property rather than a method, so destructuring it off the hook result
   * is legitimate — see the note in `useDistanceUnit`.
   */
  readonly intervalFor: (intent: LegIntent) => number | null
  /**
   * Whether this intent routes through an engine that reports surface, or `null` when the table
   * cannot say.
   *
   * Resolved intent → provider → `reports_surface`, never a list kept in the frontend. A
   * hand-kept one went stale the day the policy table repointed an intent and produced an
   * entirely grey route with no explanation. Measured live: `twisty_paved` resolves to Google,
   * which returns zero spans, so 229 of 269 km of a real trip rendered `unknown`.
   *
   * `null` rather than `false` for the three unknowns — not loaded, intent absent, provider
   * absent — because "we cannot tell you" and "this mode will not tell you" are different
   * things to put in front of a rider.
   */
  readonly reportsSurface: (intent: LegIntent) => boolean | null
  /**
   * Whether this intent's engine reports a riding time worth believing, or `null` when the table
   * cannot say.
   *
   * A capability rather than a rule, because the two engines fail in opposite directions. Hosted
   * ORS routes dirt through a bicycle profile and reported 143 min for a 40 km leg that takes 46;
   * Google runs a car profile and reported 128 min for 177 km of highway where our own speed
   * table said 193. So for one our model is the better number and for the other it is worse.
   *
   * This is *provenance*, not quality. A mode answering `false` is one where the figure shown is
   * our estimate — which on dirt is the more accurate of the two.
   */
  readonly reportsTrustworthyDuration: (intent: LegIntent) => boolean | null
  /**
   * Whether this intent's engine measures elevation, or `null` when the table cannot say.
   *
   * Resolved intent to provider to capability, like `reportsSurface` — elevation is a property of
   * the engine, and the intent table names which engine serves each intent. Google reports none,
   * which is why a mixed trip's climb figure covers only part of the route.
   */
  readonly reportsElevation: (intent: LegIntent) => boolean | null
}

export function useRoutingCapabilities(client: CapabilitiesReader): RoutingCapabilitiesState {
  const [capabilities, setCapabilities] = useState<RoutingCapabilitiesResponse | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()

    client.routingCapabilities({ signal: controller.signal }).then(
      (response) => {
        if (!cancelled) setCapabilities(response)
      },
      (reason: unknown) => {
        if (cancelled || controller.signal.aborted) return
        setError(reason instanceof Error ? reason : new Error(String(reason)))
      },
    )

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [client])

  // Memoised because callers key effects and long-lived objects on this. Returning a fresh
  // object every render is how a DragSession came to be rebuilt mid-gesture, losing the
  // gesture with it.
  return useMemo(
    () => ({
      capabilities,
      isLoaded: capabilities !== null,
      error,
      reportsTrustworthyDuration: (intent: LegIntent): boolean | null =>
        // Straight off the intent table: the backend resolves intent to provider to capability,
        // so there is nothing for the frontend to work out and nothing to go stale.
        capabilities?.intents[intent]?.reports_trustworthy_duration ?? null,
      reportsElevation: (intent: LegIntent): boolean | null => {
        const provider = capabilities?.intents[intent]?.provider
        if (provider === undefined) return null
        return capabilities?.providers.find((each) => each.name === provider)?.elevation ?? null
      },
      reportsSurface: (intent: LegIntent): boolean | null => {
        const provider = capabilities?.intents[intent]?.provider
        if (provider === undefined) return null
        return capabilities?.providers.find((each) => each.name === provider)?.reports_surface ?? null
      },
      intervalFor: (intent: LegIntent): number | null =>
        // `?? null` collapses three different unknowns — not loaded, failed, intent absent
        // from the table — into the one safe answer. An interval the API did not authorise
        // is the only outcome that cannot be allowed.
        capabilities?.intents[intent]?.live_update_interval_ms ?? null,
    }),
    [capabilities, error],
  )
}
