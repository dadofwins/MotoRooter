import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useRoutingCapabilities } from './useRoutingCapabilities'
import type { RequestOptions } from '../api/client'
import type { RoutingCapabilitiesResponse } from '../api/types'
import { intentRouting, providerCapabilities } from '../api/fixtures'

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
    providerCapabilities({
      name: 'ors',
      prefers_unpaved: true,
      reports_surface: true,
      alternatives: true,
      elevation: true,
      live_update_interval_ms: 3000,
      daily_quota: 2000,
    }),
  ],
  intents: {
    unpaved: intentRouting({ provider: 'ors', live_update_interval_ms: 3000 }),
    highway_connector: intentRouting({ provider: 'google', live_update_interval_ms: 1000 }),
    manual_track: intentRouting({ provider: 'ors', live_update_interval_ms: null }),
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

/**
 * Whether a mode costs the rider their surface breakdown.
 *
 * `CLAUDE.md` is explicit that this must not be a hand-kept list: it went stale the day the
 * policy table repointed an intent, and the result was an entirely grey route. So the answer is
 * resolved through the table — intent to provider, provider to `reports_surface` — and the
 * picker can tell a rider at the moment of choosing rather than after.
 */
describe('useRoutingCapabilities.reportsSurface', () => {
  it('resolves through the intent table to the provider that serves it', async () => {
    const { result } = renderHook(() => useRoutingCapabilities(fakeClient()))

    await waitFor(() => {
      expect(result.current.isLoaded).toBe(true)
    })
    expect(result.current.reportsSurface('unpaved')).toBe(true)
  })

  it('says a mode does not report surface when its provider cannot', async () => {
    // Measured live: `twisty_paved` resolves to Google, which returned zero spans over 270 km,
    // so 229 of 269 km of a real trip rendered grey.
    const { result } = renderHook(() =>
      useRoutingCapabilities(
        fakeClient({
          ...CAPABILITIES,
          providers: [
            ...CAPABILITIES.providers,
            providerCapabilities({
              name: 'google',
              alternatives: true,
              max_waypoints: 25,
              live_update_interval_ms: 1000,
            }),
          ],
        }),
      ),
    )

    await waitFor(() => {
      expect(result.current.isLoaded).toBe(true)
    })
    expect(result.current.reportsSurface('highway_connector')).toBe(false)
  })

  it('does not guess when the table cannot answer', async () => {
    // Three different unknowns — not loaded, intent absent, provider absent — and none of them
    // is "yes". Claiming a mode reports surface when it does not is how a rider ends up looking
    // at a grey route with no explanation.
    const { result } = renderHook(() => useRoutingCapabilities(fakeClient()))

    expect(result.current.reportsSurface('unpaved')).toBeNull()
    await waitFor(() => {
      expect(result.current.isLoaded).toBe(true)
    })
    // The intent table names google, but no such provider is listed.
    expect(result.current.reportsSurface('highway_connector')).toBeNull()
    expect(result.current.reportsSurface('technical_offroad')).toBeNull()
  })
})

/**
 * Whether a mode's own duration can be believed.
 *
 * The distinction exists because the two engines fail in opposite directions. Hosted ORS routes
 * dirt through a bicycle profile and reported 143 min for a 40 km leg that takes 46; Google runs a
 * car profile and reported 128 min for 177 km of highway where our speed table said 193. So for
 * one engine our model is the better number and for the other it is worse, which is why this is a
 * capability and not a global rule.
 */
describe('useRoutingCapabilities.reportsTrustworthyDuration', () => {
  it('answers from the intent table rather than from a provider name', async () => {
    const { result } = renderHook(() =>
      useRoutingCapabilities(
        fakeClient({
          ...CAPABILITIES,
          intents: {
            unpaved: {
              provider: 'ors',
              live_update_interval_ms: 3000,
              reports_trustworthy_duration: false,
            },
            highway_connector: {
              provider: 'google',
              live_update_interval_ms: 1000,
              reports_trustworthy_duration: true,
            },
          },
        }),
      ),
    )

    await waitFor(() => {
      expect(result.current.isLoaded).toBe(true)
    })
    expect(result.current.reportsTrustworthyDuration('highway_connector')).toBe(true)
    expect(result.current.reportsTrustworthyDuration('unpaved')).toBe(false)
  })

  it('does not guess before the table has arrived', async () => {
    // Null, not false. Saying "this mode's times are our estimate" before we know is a claim
    // about the engine we have not yet been told anything about.
    const { result } = renderHook(() => useRoutingCapabilities(fakeClient()))

    expect(result.current.reportsTrustworthyDuration('unpaved')).toBeNull()

    await waitFor(() => {
      expect(result.current.isLoaded).toBe(true)
    })
    expect(result.current.reportsTrustworthyDuration('nonexistent' as 'unpaved')).toBeNull()
  })
})
