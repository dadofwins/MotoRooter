import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useRouteLegs } from './useRouteLegs'
import { routeLeg, routeLegResponse, tripLeg } from '../api/fixtures'
import { legsSpanning } from '../routing/legStructure'
import type { RequestOptions } from '../api/client'
import type {
  Coordinate,
  RouteLegInput,
  RouteLegResponse,
  TripLeg,
  Waypoint,
} from '../api/types'

/**
 * Routing a trip that is made of legs.
 *
 * The behaviour worth pinning is not "it fetches a route" — it is **which legs an edit makes
 * it fetch.** Every provider call is metered and the ORS free tier is roughly 2,000 a day, so
 * a hook that re-routes a leg the rider did not touch is not slightly wasteful, it is the
 * thing that makes the app stop working halfway through an afternoon.
 *
 * The other half is partial failure. A trip with one unroutable segment still has every other
 * segment, and blanking the map because one leg failed throws away work the rider can use.
 */

function waypoint(lat: number): Waypoint {
  return { coordinate: { lat, lon: -120 }, name: null, pinned: true }
}

/** Waypoints far enough apart that each leg is visibly its own request. */
const THREE: readonly Waypoint[] = [waypoint(47), waypoint(48), waypoint(49)]

function response(geometry: readonly Coordinate[], durationS = 900): RouteLegResponse {
  return routeLegResponse({
    leg: routeLeg({ geometry: [...geometry], distance_m: 1000, intent: 'unpaved' }),
    estimated_duration_s: durationS,
  })
}

/** Answers each request with geometry that names the waypoints it was asked about. */
function fakeClient() {
  return {
    routeLeg: vi.fn((request: RouteLegInput, _options?: RequestOptions) =>
      Promise.resolve(response(request.waypoints)),
    ),
  }
}

/** The latitudes each call asked about, so a test can say which legs were routed. */
function requestedLats(client: ReturnType<typeof fakeClient>): number[][] {
  return client.routeLeg.mock.calls.map((call) => call[0].waypoints.map((point) => point.lat))
}

describe('useRouteLegs', () => {
  it('routes each leg separately rather than the whole trip at once', async () => {
    // The reason the branch exists. One request spanning every waypoint means one intent for
    // the whole trip, and a trip that is highway then dirt then highway cannot be expressed.
    const client = fakeClient()
    const legs = legsSpanning(3, 'unpaved')

    renderHook(() => useRouteLegs(client, THREE, legs))

    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(2)
    })
    expect(requestedLats(client).sort()).toEqual([
      [47, 48],
      [48, 49],
    ])
  })

  it('routes nothing when there is nothing to route between', () => {
    const client = fakeClient()

    renderHook(() => useRouteLegs(client, [waypoint(47)], []))

    expect(client.routeLeg).not.toHaveBeenCalled()
  })

  it('sends each leg its own intent', async () => {
    // Inert until now: `TripLeg.intent` existed but one leg meant one intent for everything.
    const client = fakeClient()
    const legs: TripLeg[] = [
      tripLeg({ intent: 'highway_connector', start_waypoint_index: 0, end_waypoint_index: 1, routed: null }),
      tripLeg({ intent: 'unpaved', start_waypoint_index: 1, end_waypoint_index: 2, routed: null }),
    ]

    renderHook(() => useRouteLegs(client, THREE, legs))

    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(2)
    })
    const intents = client.routeLeg.mock.calls.map((call) => call[0].intent)
    expect(intents).toContain('highway_connector')
    expect(intents).toContain('unpaved')
  })

  it('gives back the structure it was handed, with geometry filled in', async () => {
    const client = fakeClient()
    const legs = legsSpanning(3, 'unpaved')

    const view = renderHook(() => useRouteLegs(client, THREE, legs))

    await waitFor(() => {
      expect(view.result.current.legs.every((leg) => leg.routed !== null)).toBe(true)
    })
    expect(view.result.current.legs.map((leg) => [leg.start_waypoint_index, leg.end_waypoint_index])).toEqual(
      [
        [0, 1],
        [1, 2],
      ],
    )
  })

  it('routes only the new leg when a waypoint is appended', async () => {
    // The whole point. This used to cost one request for the entire trip on every click, so
    // the bill and the wait both grew with the length of the trip.
    const client = fakeClient()
    const view = renderHook(
      ({ waypoints, legs }: { waypoints: readonly Waypoint[]; legs: readonly TripLeg[] }) =>
        useRouteLegs(client, waypoints, legs),
      { initialProps: { waypoints: [...THREE.slice(0, 2)], legs: legsSpanning(2, 'unpaved') } },
    )
    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(1)
    })

    view.rerender({ waypoints: [...THREE], legs: legsSpanning(3, 'unpaved') })

    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(2)
    })
    // Not three: the first leg was already answered and is unchanged.
    expect(requestedLats(client)[1]).toEqual([48, 49])
  })

  it('keeps the geometry of legs an edit did not touch', async () => {
    const client = fakeClient()
    const view = renderHook(
      ({ waypoints, legs }: { waypoints: readonly Waypoint[]; legs: readonly TripLeg[] }) =>
        useRouteLegs(client, waypoints, legs),
      { initialProps: { waypoints: [...THREE.slice(0, 2)], legs: legsSpanning(2, 'unpaved') } },
    )
    await waitFor(() => {
      expect(view.result.current.legs[0]?.routed).not.toBeNull()
    })
    const first = view.result.current.legs[0]?.routed

    view.rerender({ waypoints: [...THREE], legs: legsSpanning(3, 'unpaved') })

    // Still drawn, and still the same geometry object: the rider sees no flicker on the part
    // of the route they did not change.
    expect(view.result.current.legs[0]?.routed).toBe(first)
  })

  it('makes one request per leg, not one per render', async () => {
    const client = fakeClient()
    const legs = legsSpanning(3, 'unpaved')
    const view = renderHook(() => useRouteLegs(client, [...THREE], [...legs]))

    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(2)
    })
    // Fresh arrays each time, which is what a parent deriving them does on every render.
    view.rerender()
    view.rerender()

    expect(client.routeLeg).toHaveBeenCalledTimes(2)
  })

  it('does not re-request a leg it has already answered, even after going away and back', async () => {
    const client = fakeClient()
    const view = renderHook(
      ({ waypoints, legs }: { waypoints: readonly Waypoint[]; legs: readonly TripLeg[] }) =>
        useRouteLegs(client, waypoints, legs),
      { initialProps: { waypoints: [...THREE], legs: legsSpanning(3, 'unpaved') } },
    )
    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(2)
    })

    // Removing the last point and putting it back is an undo, and undo should be free.
    view.rerender({ waypoints: [...THREE.slice(0, 2)], legs: legsSpanning(2, 'unpaved') })
    view.rerender({ waypoints: [...THREE], legs: legsSpanning(3, 'unpaved') })

    await waitFor(() => {
      expect(view.result.current.legs.every((leg) => leg.routed !== null)).toBe(true)
    })
    expect(client.routeLeg).toHaveBeenCalledTimes(2)
  })

  it('does not re-request a leg the drag already routed', async () => {
    // A drag routes on release, so the hook must recognise geometry it is handed. Freshness
    // comes from `routed_from`, not from who called last.
    const client = fakeClient()
    const dragged: TripLeg = tripLeg({
      start_waypoint_index: 0,
      end_waypoint_index: 1,
      intent: 'unpaved',
      routed: routeLeg({
        intent: 'unpaved',
        routed_from: { intent: 'unpaved', waypoints: [waypoint(47).coordinate, waypoint(48).coordinate] },
      }),
    })

    renderHook(() => useRouteLegs(client, THREE.slice(0, 2), [dragged]))

    // Give any request a chance to be made before concluding none was.
    await waitFor(() => {
      expect(client.routeLeg).not.toHaveBeenCalled()
    })
  })

  it('re-routes a leg whose geometry no longer matches its waypoints', async () => {
    const client = fakeClient()
    const moved: TripLeg = tripLeg({
      start_waypoint_index: 0,
      end_waypoint_index: 1,
      intent: 'unpaved',
      routed: routeLeg({
        intent: 'unpaved',
        // Routed between somewhere else entirely.
        routed_from: { intent: 'unpaved', waypoints: [{ lat: 10, lon: 10 }, { lat: 11, lon: 11 }] },
      }),
    })

    renderHook(() => useRouteLegs(client, THREE.slice(0, 2), [moved]))

    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(1)
    })
  })

  it('re-routes a leg whose intent changed, without touching its neighbour', async () => {
    // What the routing-mode picker will do. Changing one segment to Fast must not re-route
    // the dirt section beside it.
    const client = fakeClient()
    const view = renderHook(
      ({ legs }: { legs: readonly TripLeg[] }) => useRouteLegs(client, THREE, legs),
      { initialProps: { legs: legsSpanning(3, 'unpaved') } },
    )
    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(2)
    })

    const repointed = legsSpanning(3, 'unpaved')
    view.rerender({
      legs: repointed.map((leg, index) => (index === 0 ? { ...leg, intent: 'highway_connector' as const } : leg)),
    })

    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(3)
    })
    expect(client.routeLeg.mock.calls[2]?.[0].intent).toBe('highway_connector')
    expect(requestedLats(client)[2]).toEqual([47, 48])
  })
})

describe('partial failure', () => {
  /** Fails the leg starting at `failAt`, answers everything else. */
  function clientFailing(failAt: number) {
    return {
      routeLeg: vi.fn((request: RouteLegInput, _options?: RequestOptions) =>
        request.waypoints[0]?.lat === failAt
          ? Promise.reject(new Error('no route found'))
          : Promise.resolve(response(request.waypoints)),
      ),
    }
  }

  it('keeps the legs that did route', async () => {
    // The same partial-versus-total argument the backend settled on the resolver: one segment
    // failing is not the trip failing, and throwing the rest away discards usable work.
    const client = clientFailing(48)
    const view = renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))

    await waitFor(() => {
      expect(view.result.current.legs[0]?.routed).not.toBeNull()
    })
    expect(view.result.current.legs[1]?.routed).toBeNull()
  })

  it('says how many segments it could not route', async () => {
    const client = clientFailing(48)
    const view = renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))

    await waitFor(() => {
      expect(view.result.current.unroutableCount).toBe(1)
    })
    expect(view.result.current.error).not.toBeNull()
  })

  it('stops reporting a failure once the leg that caused it is gone', async () => {
    const client = clientFailing(48)
    const view = renderHook(
      ({ waypoints, legs }: { waypoints: readonly Waypoint[]; legs: readonly TripLeg[] }) =>
        useRouteLegs(client, waypoints, legs),
      { initialProps: { waypoints: [...THREE], legs: legsSpanning(3, 'unpaved') } },
    )
    await waitFor(() => {
      expect(view.result.current.unroutableCount).toBe(1)
    })

    view.rerender({ waypoints: [...THREE.slice(0, 2)], legs: legsSpanning(2, 'unpaved') })

    // Otherwise an alert about a segment the rider has deleted cannot be dismissed.
    expect(view.result.current.unroutableCount).toBe(0)
    expect(view.result.current.error).toBeNull()
  })

  it('does not retry a failed leg because a sibling succeeded', async () => {
    // A tempting bug: watch the cache, re-run on every change, and each success re-fires
    // every leg that failed. On a trip with one dead segment that is a request per success.
    const client = clientFailing(48)
    renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))

    await waitFor(() => {
      expect(client.routeLeg).toHaveBeenCalledTimes(2)
    })
    await waitFor(() => {
      expect(requestedLats(client)).toHaveLength(2)
    })
    expect(requestedLats(client).filter((lats) => lats[0] === 48)).toHaveLength(1)
  })

  it('retries a failed leg when the rider changes it', async () => {
    // A failure is not an answer to be cached. Editing the segment asks again.
    const client = clientFailing(48)
    const view = renderHook(
      ({ waypoints, legs }: { waypoints: readonly Waypoint[]; legs: readonly TripLeg[] }) =>
        useRouteLegs(client, waypoints, legs),
      { initialProps: { waypoints: [...THREE], legs: legsSpanning(3, 'unpaved') } },
    )
    await waitFor(() => {
      expect(view.result.current.unroutableCount).toBe(1)
    })

    view.rerender({
      waypoints: [waypoint(47), waypoint(48), waypoint(50)],
      legs: legsSpanning(3, 'unpaved'),
    })

    await waitFor(() => {
      expect(requestedLats(client).filter((lats) => lats[0] === 48)).toHaveLength(2)
    })
  })
})

describe('what the rail shows', () => {
  it('adds up the estimated riding time of every routed leg', async () => {
    // Per-leg estimates, derived server-side from distance and surface. Never the provider's
    // own figure: hosted ORS routes dirt through a bicycle profile and reported 2.31 h for a
    // 39 km leg that takes 45 minutes.
    const client = {
      routeLeg: vi.fn((request: RouteLegInput, _options?: RequestOptions) =>
        Promise.resolve(response(request.waypoints, 600)),
      ),
    }
    const view = renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))

    await waitFor(() => {
      expect(view.result.current.estimatedDurationS).toBe(1200)
    })
  })

  it('has no estimate before anything has routed', () => {
    const client = fakeClient()
    const view = renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))

    // Null rather than zero: zero is a duration, and would render as "under 5m" for a trip
    // nobody has estimated.
    expect(view.result.current.estimatedDurationS).toBeNull()
  })

  it('does not report a partial time as the trip\'s time', async () => {
    // Half a trip's riding time shown as the trip's riding time is worse than no figure: it
    // is the number a rider plans a day around, and it would read as plausible.
    const client = {
      routeLeg: vi.fn((request: RouteLegInput, _options?: RequestOptions) =>
        request.waypoints[0]?.lat === 48
          ? Promise.reject(new Error('no route found'))
          : Promise.resolve(response(request.waypoints, 600)),
      ),
    }
    const view = renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))

    await waitFor(() => {
      expect(view.result.current.unroutableCount).toBe(1)
    })
    expect(view.result.current.estimatedDurationS).toBeNull()
  })

  it('leaves the total to the trip document for a trip it did not route', async () => {
    // A trip restored from storage has geometry but no per-leg estimate — the backend computed
    // one figure for the whole document. Summing what this hook happens to hold would report
    // whatever the rider has just re-routed as the length of a three-day trip.
    const client = fakeClient()
    const loaded: TripLeg = tripLeg({
      start_waypoint_index: 0,
      end_waypoint_index: 1,
      intent: 'unpaved',
      routed: routeLeg({
        intent: 'unpaved',
        duration_s: 99_999,
        routed_from: {
          intent: 'unpaved',
          waypoints: [waypoint(47).coordinate, waypoint(48).coordinate],
        },
      }),
    })

    const view = renderHook(() => useRouteLegs(client, THREE.slice(0, 2), [loaded]))

    await waitFor(() => {
      expect(client.routeLeg).not.toHaveBeenCalled()
    })
    // Never the provider's own duration: on dirt that is a bicycle time.
    expect(view.result.current.estimatedDurationS).toBeNull()
  })

  it('reports that it is working while any leg is in flight', async () => {
    const client = fakeClient()
    const view = renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))

    expect(view.result.current.isRouting).toBe(true)
    await waitFor(() => {
      expect(view.result.current.isRouting).toBe(false)
    })
  })

  it('is not working when every leg has settled, failures included', async () => {
    const client = {
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(new Error('no route found')),
      ),
    }
    const view = renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))

    await waitFor(() => {
      expect(view.result.current.isRouting).toBe(false)
    })
    // Otherwise the rail spins forever on a trip that will never route.
    expect(view.result.current.unroutableCount).toBe(2)
  })
})

describe('quota', () => {
  it('aborts a request for a leg the rider has removed', async () => {
    const signals: AbortSignal[] = []
    const client = {
      routeLeg: vi.fn((request: RouteLegInput, options?: RequestOptions) => {
        if (options?.signal !== undefined) signals.push(options.signal)
        return new Promise<RouteLegResponse>((resolve) => {
          setTimeout(() => resolve(response(request.waypoints)), 50)
        })
      }),
    }
    const view = renderHook(
      ({ waypoints, legs }: { waypoints: readonly Waypoint[]; legs: readonly TripLeg[] }) =>
        useRouteLegs(client, waypoints, legs),
      { initialProps: { waypoints: [...THREE], legs: legsSpanning(3, 'unpaved') } },
    )
    await waitFor(() => {
      expect(signals).toHaveLength(2)
    })

    view.rerender({ waypoints: [...THREE.slice(0, 2)], legs: legsSpanning(2, 'unpaved') })

    // The second leg is gone, so its answer is worthless and the request should stop.
    await waitFor(() => {
      expect(signals[1]?.aborted).toBe(true)
    })
  })

  it('leaves a request alone when its leg survives the edit', async () => {
    const signals: AbortSignal[] = []
    const client = {
      routeLeg: vi.fn((request: RouteLegInput, options?: RequestOptions) => {
        if (options?.signal !== undefined) signals.push(options.signal)
        return new Promise<RouteLegResponse>((resolve) => {
          setTimeout(() => resolve(response(request.waypoints)), 50)
        })
      }),
    }
    const view = renderHook(
      ({ waypoints, legs }: { waypoints: readonly Waypoint[]; legs: readonly TripLeg[] }) =>
        useRouteLegs(client, waypoints, legs),
      { initialProps: { waypoints: [...THREE.slice(0, 2)], legs: legsSpanning(2, 'unpaved') } },
    )
    await waitFor(() => {
      expect(signals).toHaveLength(1)
    })

    // Appending a third point does not change the first leg.
    view.rerender({ waypoints: [...THREE], legs: legsSpanning(3, 'unpaved') })

    // Aborting and refiring it would be two requests for one answer — the classic
    // "cancel everything on any change" bug, paid for in quota.
    await waitFor(() => {
      expect(signals).toHaveLength(2)
    })
    expect(signals[0]?.aborted).toBe(false)
    expect(client.routeLeg.mock.calls.filter((call) => call[0].waypoints[0]?.lat === 47)).toHaveLength(1)
  })

  it('abandons everything in flight when it unmounts', async () => {
    const signals: AbortSignal[] = []
    const client = {
      routeLeg: vi.fn((request: RouteLegInput, options?: RequestOptions) => {
        if (options?.signal !== undefined) signals.push(options.signal)
        return new Promise<RouteLegResponse>((resolve) => {
          setTimeout(() => resolve(response(request.waypoints)), 50)
        })
      }),
    }
    const view = renderHook(() => useRouteLegs(client, THREE, legsSpanning(3, 'unpaved')))
    await waitFor(() => {
      expect(signals).toHaveLength(2)
    })

    view.unmount()

    expect(signals.every((signal) => signal.aborted)).toBe(true)
  })
})
