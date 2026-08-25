import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SURFACE_REPORTING_INTENTS, useRouteLeg } from './useRouteLeg'
import type { RequestOptions } from '../api/client'
import type { Coordinate, RouteLeg, RouteLegInput, RouteLegResponse, TripLeg, Waypoint } from '../api/types'

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

describe('legs supplied by a drag', () => {
  /**
   * A drag routes the leg itself, on release. If the hook then re-routed the same waypoints
   * it would spend a second request for geometry the app is already holding — once per
   * drag, against a free tier of roughly 2,000 a day.
   *
   * Freshness is decided from `RouteLeg.routed_from` rather than from who called last.
   */
  function fresh(from: readonly Coordinate[]): TripLeg {
    return {
      intent: 'unpaved',
      start_waypoint_index: 0,
      end_waypoint_index: from.length - 1,
      provider_override: null,
      routed: {
        ...routed(from),
        intent: 'unpaved',
        routed_from: { intent: 'unpaved', waypoints: [...from] },
      },
    }
  }

  it('uses them instead of re-requesting a route already in hand', async () => {
    const client = fakeClient()
    const points = [waypoint(47), waypoint(47.5), waypoint(48)]
    const known = [fresh(points.map((p) => p.coordinate))]

    const { result } = renderHook(() => useRouteLeg(client, points, known))

    await waitFor(() => expect(result.current.isRouting).toBe(false))
    expect(client.routeLeg).not.toHaveBeenCalled()
    expect(result.current.legs).toBe(known)
  })

  it('re-requests when they no longer match the waypoints', async () => {
    // The state right after a via-point is inserted and before its route comes back.
    const client = fakeClient()
    const stale = [fresh([{ lat: 47, lon: -120 }, { lat: 48, lon: -120 }])]
    const points = [waypoint(47), waypoint(47.5), waypoint(48)]

    renderHook(() => useRouteLeg(client, points, stale))

    await waitFor(() => expect(client.routeLeg).toHaveBeenCalledTimes(1))
  })
})

describe('the default routing intent', () => {
  it('is one that routes through an engine able to report surface', async () => {
    // The bug this guards: `twisty_paved` resolves to Google, which exposes no surface data
    // at all, so a 270 km route came back with zero spans and rendered as one uniform grey
    // line. Correct — unknown is not paved — and completely useless, because the paved
    // versus dirt distinction is the entire reason this app exists. Invisible in every unit
    // test, obvious in one second of use.
    const client = fakeClient()

    const { result } = renderHook(() => useRouteLeg(client, [waypoint(47), waypoint(48)]))
    await waitFor(() => expect(result.current.legs).toHaveLength(1))

    const intent = client.routeLeg.mock.calls[0]?.[0].intent
    expect(SURFACE_REPORTING_INTENTS).toContain(intent)
  })
})

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
