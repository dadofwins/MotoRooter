import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useRoutingCapabilities } from './useRoutingCapabilities'
import type { RequestOptions } from '../api/client'
import type { RoutingCapabilitiesResponse } from '../api/types'

/**
 * Where the drag cadence comes from.
 *
 * Never a constant in the frontend: a cheap engine can refresh near-live while a metered one
 * must hold off, and which engine serves an intent is the backend's policy table to decide.
 * Hardcoding an interval here would silently diverge from whatever is actually serving the
 * leg — and on ORS's free tier, diverging in the wrong direction exhausts the day's quota in
 * one session.
 */

const CAPABILITIES: RoutingCapabilitiesResponse = {
  providers: [
    {
      name: 'ors',
      prefers_unpaved: true,
      map_matching: false,
      alternatives: true,
      elevation: true,
      max_waypoints: 50,
      live_update_interval_ms: 3000,
      daily_quota: 2000,
    },
  ],
  intents: {
    unpaved: { provider: 'ors', live_update_interval_ms: 3000 },
    highway_connector: { provider: 'google', live_update_interval_ms: 1000 },
    manual_track: { provider: 'ors', live_update_interval_ms: null },
  },
}

function fakeClient(response: RoutingCapabilitiesResponse = CAPABILITIES) {
  return {
    routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(response)),
  }
}

describe('useRoutingCapabilities', () => {
  it('reports the interval the API gives for an intent', async () => {
    const client = fakeClient()

    const { result } = renderHook(() => useRoutingCapabilities(client))

    await waitFor(() => expect(result.current.isLoaded).toBe(true))
    expect(result.current.intervalFor('unpaved')).toBe(3000)
    expect(result.current.intervalFor('highway_connector')).toBe(1000)
  })

  it('passes through preview-only, which is a real setting and not a missing value', () => {
    // `null` means: rubber-band locally and route only on release. A metered engine is
    // entitled to say that, and it must not be confused with "not known yet".
    const client = fakeClient()

    const { result } = renderHook(() => useRoutingCapabilities(client))

    return waitFor(() => {
      expect(result.current.intervalFor('manual_track')).toBeNull()
    })
  })

  it('is preview-only before the answer arrives', () => {
    const client = {
      routingCapabilities: vi.fn(
        (_options?: RequestOptions) => new Promise<RoutingCapabilitiesResponse>(() => undefined),
      ),
    }

    const { result } = renderHook(() => useRoutingCapabilities(client))

    // Routing at a cadence nobody has authorised is the one option not available here.
    expect(result.current.intervalFor('unpaved')).toBeNull()
    expect(result.current.isLoaded).toBe(false)
  })

  it('is preview-only for an intent the API said nothing about', async () => {
    const client = fakeClient()

    const { result } = renderHook(() => useRoutingCapabilities(client))
    await waitFor(() => expect(result.current.isLoaded).toBe(true))

    expect(result.current.intervalFor('technical_offroad')).toBeNull()
  })

  it('is preview-only when the request fails, rather than falling back to a guess', async () => {
    const client = {
      routingCapabilities: vi.fn((_options?: RequestOptions) =>
        Promise.reject(new Error('offline')),
      ),
    }

    const { result } = renderHook(() => useRoutingCapabilities(client))

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.intervalFor('unpaved')).toBeNull()
  })

  it('asks once, however often it re-renders', async () => {
    const client = fakeClient()

    const { result, rerender } = renderHook(() => useRoutingCapabilities(client))
    await waitFor(() => expect(result.current.isLoaded).toBe(true))
    rerender()
    rerender()

    expect(client.routingCapabilities).toHaveBeenCalledTimes(1)
  })

  it('abandons the request if it is unmounted first', () => {
    const signals: AbortSignal[] = []
    const client = {
      routingCapabilities: vi.fn((options?: RequestOptions) => {
        if (options?.signal !== undefined) signals.push(options.signal)
        return new Promise<RoutingCapabilitiesResponse>(() => undefined)
      }),
    }

    const { unmount } = renderHook(() => useRoutingCapabilities(client))
    unmount()

    expect(signals[0]?.aborted).toBe(true)
  })
})
