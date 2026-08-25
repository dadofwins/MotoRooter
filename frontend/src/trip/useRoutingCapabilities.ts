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
import { useEffect, useState } from 'react'
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
   */
  intervalFor(intent: LegIntent): number | null
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

  return {
    capabilities,
    isLoaded: capabilities !== null,
    error,
    intervalFor(intent: LegIntent): number | null {
      // `?? null` collapses three different unknowns — not loaded, failed, intent absent
      // from the table — into the one safe answer. An interval the API did not authorise is
      // the only outcome that cannot be allowed.
      return capabilities?.intents[intent]?.live_update_interval_ms ?? null
    },
  }
}
