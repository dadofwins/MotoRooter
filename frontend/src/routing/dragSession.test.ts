import { describe, expect, it, vi } from 'vitest'
import { DragSession, type LegRouter } from './dragSession'
import { routeLeg } from '../api/fixtures'
import type { RouteEdit } from './tripEdits'
import type { Coordinate, RouteLeg, RouteLegInput, RouteLegResponse, TripLeg, Waypoint } from '../api/types'
import type { RequestOptions } from '../api/client'

/**
 * One drag gesture, end to end: grab the line, move, release.
 *
 * This is where the pieces meet — `DragScheduler` for cadence and stale-response rejection,
 * the API client for the leg request, and the pure trip edits for the waypoint arithmetic.
 * The properties that matter are the ones a user would notice going wrong:
 *
 * - only the grabbed leg is re-requested, never the whole route;
 * - a drag inserts exactly one via-point however far it is moved;
 * - the release is authoritative, and a mid-drag preview that lands afterwards is discarded;
 * - the throttle interval comes from the caller, which got it from the API.
 */

function waypoint(lat: number): Waypoint {
  return { coordinate: { lat, lon: -120 }, name: null, pinned: true }
}

function routed(geometry: readonly Coordinate[]): RouteLeg {
  return routeLeg({ geometry: [...geometry] })
}

function leg(start: number, end: number, intent: TripLeg['intent'] = 'unpaved'): TripLeg {
  return {
    intent,
    start_waypoint_index: start,
    end_waypoint_index: end,
    provider_override: null,
    routed: routed([
      { lat: 47 + start, lon: -120 },
      { lat: 47 + start + 0.5, lon: -120 },
      { lat: 47 + end, lon: -120 },
    ]),
  }
}

/** Wraps a leg in a response, so a new required field lands in one place, not fifteen. */
function legResponse(leg: RouteLeg, live_update_interval_ms: number | null = 3000): RouteLegResponse {
  return { leg, live_update_interval_ms, estimated_duration_s: 60 }
}

const TRIP: RouteEdit = {
  waypoints: [waypoint(47), waypoint(48), waypoint(49)],
  legs: [leg(0, 1), leg(1, 2)],
}

function fakeClient(
  response: RouteLegResponse = legResponse(routed([{ lat: 0, lon: 0 }])),
) {
  return {
    // Typed parameters, so the recorded calls are checked against the real request shape
    // rather than inspected as `unknown`.
    routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) => Promise.resolve(response)),
  }
}

function session(
  client: LegRouter,
  intervalMs: number | null = 0,
): {
  drag: DragSession
  onPreview: ReturnType<typeof vi.fn>
  onCommit: ReturnType<typeof vi.fn>
  onError: ReturnType<typeof vi.fn>
} {
  const onPreview = vi.fn()
  const onCommit = vi.fn()
  const onError = vi.fn()
  return {
    drag: new DragSession({ client, intervalMs, onPreview, onCommit, onError }),
    onPreview,
    onCommit,
    onError,
  }
}

describe('DragSession', () => {
  it('re-requests only the grabbed leg, with the via-point in it', async () => {
    // Whole-route recompute is what makes an editor feel sluggish, and on a metered
    // provider it is also what burns the daily quota.
    const client = fakeClient()
    const { drag, onCommit } = session(client)

    drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.release({ lat: 47.5, lon: -120.3 })
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))

    expect(client.routeLeg).toHaveBeenCalledTimes(1)
    const request = client.routeLeg.mock.calls[0]?.[0]
    expect(request?.waypoints).toEqual([
      { lat: 47, lon: -120 }, // the leg's own start
      { lat: 47.5, lon: -120.3 }, // the new via
      { lat: 48, lon: -120 }, // the leg's own end
    ])
    expect(request?.intent).toBe('unpaved')
  })

  it('inserts exactly one via-point however far the drag travels', async () => {
    // Each move re-derives the edit from the trip as it was when the line was grabbed. Any
    // other reading would leave a trail of waypoints behind the cursor.
    const client = fakeClient()
    const { drag, onCommit } = session(client)

    drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.update({ lat: 47.5, lon: -120.1 })
    drag.update({ lat: 47.5, lon: -120.2 })
    drag.release({ lat: 47.5, lon: -120.3 })
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))

    const committed = onCommit.mock.calls[0]?.[0] as RouteEdit
    expect(committed.waypoints).toHaveLength(4)
    expect(committed.waypoints[1]?.coordinate).toEqual({ lat: 47.5, lon: -120.3 })
  })

  it('commits the new geometry into the dragged leg and leaves its neighbour alone', async () => {
    const fresh = routed([
      { lat: 47, lon: -120 },
      { lat: 47.5, lon: -120.3 },
      { lat: 48, lon: -120 },
    ])
    const client = fakeClient(legResponse(fresh))
    const { drag, onCommit } = session(client)

    drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.release({ lat: 47.5, lon: -120.3 })
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))

    const committed = onCommit.mock.calls[0]?.[0] as RouteEdit
    expect(committed.legs[0]?.routed).toBe(fresh)
    // Untouched by identity: re-routing one leg never disturbs its neighbours.
    expect(committed.legs[1]?.routed).toBe(TRIP.legs[1]?.routed)
    expect(committed.legs[1]?.start_waypoint_index).toBe(2) // shifted, not re-routed
  })

  it('keeps the leg’s own intent rather than imposing a default', async () => {
    // Per-section provider choice is a property of the data. A drag must not quietly
    // convert a technical off-road leg into a highway connector.
    const client = fakeClient()
    const { drag, onCommit } = session(client)
    const trip: RouteEdit = { ...TRIP, legs: [leg(0, 1, 'technical_offroad'), leg(1, 2)] }

    drag.begin(trip, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.release({ lat: 47.5, lon: -120.3 })
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))

    expect(client.routeLeg.mock.calls[0]?.[0].intent).toBe('technical_offroad')
  })

  it('issues no request at all while dragging when the provider is preview-only', async () => {
    // `intervalMs: null` comes from the API for a metered engine: rubber-band locally and
    // route only on release.
    const client = fakeClient()
    const { drag, onCommit, onPreview } = session(client, null)

    drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.update({ lat: 47.5, lon: -120.1 })
    drag.update({ lat: 47.5, lon: -120.2 })

    expect(client.routeLeg).not.toHaveBeenCalled()
    expect(onPreview).not.toHaveBeenCalled()

    drag.release({ lat: 47.5, lon: -120.3 })
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))
    expect(client.routeLeg).toHaveBeenCalledTimes(1)
  })

  it('aborts the superseded request when the drag releases', async () => {
    // Stale geometry landing after the commit would silently revert the user's edit, and
    // the abandoned request would spend quota for nothing.
    const signals: AbortSignal[] = []
    const client = {
      routeLeg: vi.fn((_request: RouteLegInput, options?: RequestOptions) => {
        const signal = options?.signal
        if (signal !== undefined) signals.push(signal)
        return new Promise<RouteLegResponse>((resolve, reject) => {
          signal?.addEventListener('abort', () => {
            reject(new DOMException('aborted', 'AbortError'))
          })
          setTimeout(() => resolve(legResponse(routed([{ lat: 0, lon: 0 }]), 0)), 0)
        })
      }),
    }
    const { drag, onPreview, onCommit, onError } = session(client)

    drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.update({ lat: 47.5, lon: -120.1 })
    drag.release({ lat: 47.5, lon: -120.3 })
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))

    expect(signals).toHaveLength(2)
    expect(signals[0]?.aborted).toBe(true)
    expect(onPreview).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it('delivers a mid-drag result as a preview, never as a commit', async () => {
    const client = fakeClient()
    const { drag, onPreview, onCommit } = session(client)

    drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.update({ lat: 47.5, lon: -120.1 })
    await vi.waitFor(() => expect(onPreview).toHaveBeenCalledTimes(1))

    expect(onCommit).not.toHaveBeenCalled()
  })

  it('abandons the gesture on cancel without delivering anything', async () => {
    const client = fakeClient()
    const { drag, onPreview, onCommit } = session(client)

    drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.update({ lat: 47.5, lon: -120.1 })
    drag.cancel()
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(onPreview).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('reports a routing failure without committing a half-made edit', async () => {
    const client = {
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) => Promise.reject(new Error('no route found'))),
    }
    const { drag, onCommit, onError } = session(client)

    drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })
    drag.release({ lat: 47.5, lon: -120.3 })
    await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1))

    expect(onCommit).not.toHaveBeenCalled()
  })

  it('refuses to start on a leg with no geometry, rather than guessing the order', async () => {
    // There is no line on screen to grab when a leg has not been routed. Starting anyway
    // meant falling back to offset 1, which on a multi-waypoint leg inserts the via before
    // an existing one and doubles the route back on itself.
    const client = fakeClient()
    const { drag, onCommit } = session(client)
    const unrouted: RouteEdit = {
      waypoints: [waypoint(47), waypoint(47.5), waypoint(48)],
      legs: [{ ...leg(0, 2), routed: null }],
    }

    const started = drag.begin(unrouted, { legIndex: 0, grabbed: { lat: 47.4, lon: -120 } })
    drag.release({ lat: 47.4, lon: -120.2 })
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(started).toBe(false)
    expect(client.routeLeg).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('reports that a grab on a routed leg did start', () => {
    const { drag } = session(fakeClient())

    expect(drag.begin(TRIP, { legIndex: 0, grabbed: { lat: 47.5, lon: -120 } })).toBe(true)
    expect(drag.begin(TRIP, { legIndex: 9, grabbed: { lat: 47.5, lon: -120 } })).toBe(false)
  })

  it('ignores a move or release that was never preceded by a grab', () => {
    const client = fakeClient()
    const { drag } = session(client)

    drag.update({ lat: 1, lon: 2 })
    drag.release({ lat: 1, lon: 2 })

    expect(client.routeLeg).not.toHaveBeenCalled()
  })
})
