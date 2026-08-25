import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useRouteLeg } from './useRouteLeg'
import type { RequestOptions } from '../api/client'
import type { Coordinate, RouteLeg, RouteLegInput, RouteLegResponse, Waypoint } from '../api/types'

/**
 * Routing the waypoints the user has placed.
 *
 * The vertical slice: two clicks become a leg request, and the returned geometry becomes
 * something the canvas can draw. The properties worth pinning are the ones that make it
 * behave on a metered provider and under a fast-clicking user — one request per change, no
 * request until there is something to route, and no stale response overwriting a newer one.
 */

function waypoint(lat: number): Waypoint {
  return { coordinate: { lat, lon: -120 }, name: null, pinned: true }
}

function routed(geometry: readonly Coordinate[]): RouteLeg {
  return {
    geometry: [...geometry],
    distance_m: 1234,
    duration_s: 60,
    provider: 'fake',
    intent: 'twisty_paved',
    surface_spans: [],
    ascent_m: null,
  }
}

const RESPONSE: RouteLegResponse = {
  leg: routed([
    { lat: 47, lon: -120 },
    { lat: 48, lon: -120 },
  ]),
  live_update_interval_ms: 0,
}

function fakeClient(response: RouteLegResponse = RESPONSE) {
  return {
    routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) => Promise.resolve(response)),
  }
}

describe('useRouteLeg', () => {
  it('routes nothing until there are two points to route between', () => {
    const client = fakeClient()

    const { rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [] as readonly Waypoint[] },
    })
    rerender({ points: [waypoint(47)] })

    expect(client.routeLeg).not.toHaveBeenCalled()
  })

  it('routes the two placed points and exposes the returned leg', async () => {
    const client = fakeClient()

    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [] as readonly Waypoint[] },
    })
    rerender({ points: [waypoint(47), waypoint(48)] })

    await waitFor(() => expect(result.current.legs).toHaveLength(1))
    expect(client.routeLeg).toHaveBeenCalledTimes(1)
    expect(client.routeLeg.mock.calls[0]?.[0].waypoints).toEqual([
      { lat: 47, lon: -120 },
      { lat: 48, lon: -120 },
    ])
    expect(result.current.legs[0]?.routed).toEqual(RESPONSE.leg)
  })

  it('produces a leg the canvas can draw, spanning the right waypoints', async () => {
    // The whole point of the slice: what comes back has to be renderable without a
    // translation step invented at the call site.
    const client = fakeClient()

    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [] as readonly Waypoint[] },
    })
    rerender({ points: [waypoint(47), waypoint(48)] })

    await waitFor(() => expect(result.current.legs).toHaveLength(1))
    const leg = result.current.legs[0]
    expect(leg?.start_waypoint_index).toBe(0)
    expect(leg?.end_waypoint_index).toBe(1)
    expect(leg?.routed?.geometry.length).toBeGreaterThan(1)
  })

  it('re-routes when a point is added, and asks for every point in order', async () => {
    const client = fakeClient()

    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [waypoint(47), waypoint(48)] as readonly Waypoint[] },
    })
    await waitFor(() => expect(result.current.legs).toHaveLength(1))

    rerender({ points: [waypoint(47), waypoint(48), waypoint(49)] })

    await waitFor(() => expect(client.routeLeg).toHaveBeenCalledTimes(2))
    expect(client.routeLeg.mock.calls[1]?.[0].waypoints).toHaveLength(3)
  })

  it('makes one request per change, not one per render', async () => {
    // Re-rendering for an unrelated reason must not spend provider quota.
    const client = fakeClient()
    const points: readonly Waypoint[] = [waypoint(47), waypoint(48)]

    const { rerender } = renderHook(({ p }) => useRouteLeg(client, p), {
      initialProps: { p: points },
    })
    rerender({ p: points })
    rerender({ p: points })

    await waitFor(() => expect(client.routeLeg).toHaveBeenCalledTimes(1))
  })

  it('discards a slow earlier response when a newer one has already landed', async () => {
    // Clicking twice quickly: the first route is still in flight when the second starts.
    // If the first lands afterwards it silently reverts the user's newer edit.
    const resolvers: ((response: RouteLegResponse) => void)[] = []
    const client = {
      routeLeg: vi.fn(
        (_request: RouteLegInput, _options?: RequestOptions) =>
          new Promise<RouteLegResponse>((resolve) => resolvers.push(resolve)),
      ),
    }

    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [waypoint(47), waypoint(48)] as readonly Waypoint[] },
    })
    await waitFor(() => expect(resolvers).toHaveLength(1))
    rerender({ points: [waypoint(47), waypoint(48), waypoint(49)] })
    await waitFor(() => expect(resolvers).toHaveLength(2))

    const newest = routed([
      { lat: 47, lon: -120 },
      { lat: 49, lon: -120 },
    ])
    await act(async () => {
      resolvers[1]?.({ leg: newest, live_update_interval_ms: 0 })
      await Promise.resolve()
    })
    await act(async () => {
      resolvers[0]?.(RESPONSE) // the superseded one, landing late
      await Promise.resolve()
    })

    expect(result.current.legs[0]?.routed).toEqual(newest)
  })

  it('aborts the superseded request rather than leaving it to spend quota', async () => {
    const signals: AbortSignal[] = []
    const client = {
      routeLeg: vi.fn((_request: RouteLegInput, options?: RequestOptions) => {
        if (options?.signal !== undefined) signals.push(options.signal)
        return new Promise<RouteLegResponse>(() => undefined)
      }),
    }

    const { rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [waypoint(47), waypoint(48)] as readonly Waypoint[] },
    })
    await waitFor(() => expect(signals).toHaveLength(1))
    rerender({ points: [waypoint(47), waypoint(48), waypoint(49)] })

    await waitFor(() => expect(signals).toHaveLength(2))
    expect(signals[0]?.aborted).toBe(true)
  })

  it('clears the drawn route when the points that made it are removed', async () => {
    // Otherwise the last successful route stays on screen forever: remove both waypoints
    // and an empty map still shows a line, with a distance for a route that is gone. There
    // is no way back short of reloading the page.
    const client = fakeClient()
    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [waypoint(47), waypoint(48)] as readonly Waypoint[] },
    })
    await waitFor(() => expect(result.current.legs).toHaveLength(1))

    rerender({ points: [waypoint(47)] })
    expect(result.current.legs).toHaveLength(0)

    rerender({ points: [] })
    expect(result.current.legs).toHaveLength(0)
  })

  it('clears a routing error once the points that caused it are gone', async () => {
    // Same guard, second symptom: an undismissable alert about a route with no waypoints.
    const client = {
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(new Error('no route found')),
      ),
    }
    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [waypoint(47), waypoint(48)] as readonly Waypoint[] },
    })
    await waitFor(() => expect(result.current.error).not.toBeNull())

    rerender({ points: [waypoint(47)] })

    expect(result.current.error).toBeNull()
  })

  it('keeps the old line up while a changed route is still being fetched', async () => {
    // Clearing on *every* change would blank the map between edits. Only losing a point
    // below the routable minimum removes the line.
    const client = fakeClient()
    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: [waypoint(47), waypoint(48)] as readonly Waypoint[] },
    })
    await waitFor(() => expect(result.current.legs).toHaveLength(1))

    rerender({ points: [waypoint(47), waypoint(48), waypoint(49)] })

    expect(result.current.isRouting).toBe(true)
    expect(result.current.legs).toHaveLength(1)
    // Let the second route land before the test ends, so its state update is not applied
    // to a torn-down tree.
    await waitFor(() => expect(result.current.isRouting).toBe(false))
  })

  it('does not re-request a route it is already holding', async () => {
    // Adding a via point and undoing it should cost one request, not three. Re-fetching
    // geometry already in hand is the same fault as the stale route: state not consulted.
    const client = fakeClient()
    const two: readonly Waypoint[] = [waypoint(47), waypoint(48)]
    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: two },
    })
    await waitFor(() => expect(result.current.legs).toHaveLength(1))

    rerender({ points: [waypoint(47), waypoint(48), waypoint(49)] })
    await waitFor(() => expect(client.routeLeg).toHaveBeenCalledTimes(2))
    rerender({ points: [waypoint(47), waypoint(48)] }) // undo, back to a known route

    await waitFor(() => expect(result.current.isRouting).toBe(false))
    expect(client.routeLeg).toHaveBeenCalledTimes(2)
    expect(result.current.legs).toHaveLength(1)
  })

  it('retries a route that previously failed, rather than caching the failure', async () => {
    let attempt = 0
    const client = {
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) => {
        attempt += 1
        return attempt === 1 ? Promise.reject(new Error('boom')) : Promise.resolve(RESPONSE)
      }),
    }
    const two: readonly Waypoint[] = [waypoint(47), waypoint(48)]
    const { result, rerender } = renderHook(({ points }) => useRouteLeg(client, points), {
      initialProps: { points: two },
    })
    await waitFor(() => expect(result.current.error).not.toBeNull())

    rerender({ points: [waypoint(47)] }) // drop below two, then back
    rerender({ points: [waypoint(47), waypoint(48)] })

    await waitFor(() => expect(result.current.legs).toHaveLength(1))
  })

  it('surfaces a routing failure instead of leaving a half-drawn route', async () => {
    const client = {
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(new Error('no route found')),
      ),
    }

    const { result } = renderHook(() => useRouteLeg(client, [waypoint(47), waypoint(48)]))

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.legs).toHaveLength(0)
  })

  it('reports while a route is in flight, so the UI can say it is working', async () => {
    const client = {
      routeLeg: vi.fn(
        (_request: RouteLegInput, _options?: RequestOptions) =>
          new Promise<RouteLegResponse>(() => undefined),
      ),
    }

    const { result } = renderHook(() => useRouteLeg(client, [waypoint(47), waypoint(48)]))

    await waitFor(() => expect(result.current.isRouting).toBe(true))
  })
})
