import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useTripBlurb } from './useTripBlurb'
import { poi, routeLeg, trip as tripFixture, tripLeg } from '../api/fixtures'
import { ApiNetworkError, ApiNotImplementedError } from '../api/errors'
import type { Trip, Waypoint } from '../api/types'

/**
 * The rail header's line, and the two rules that govern it.
 *
 * **Nothing may ever wait on it.** It is decoration on a component the fast path runs
 * through, so a failure, a 501, a null answer and a slow reply all mean one thing: the caller
 * keeps the static header. There is no error state, no spinner, and nothing a rider can see
 * go wrong — which is why every failure test here asserts the same outcome.
 *
 * **It must not be regenerated on every edit.** A drag emits route updates continuously, and
 * one model call per update is the quota failure the drag throttle exists to prevent. The
 * hook is given the *committed* document only, and asks again only when `tripBlurbKey` moves.
 */

function waypoint(name: string): Waypoint {
  return { coordinate: { lat: 47, lon: -120 }, name, pinned: false }
}

function withPoints(...names: string[]): Trip {
  return tripFixture({
    waypoints: names.map(waypoint),
    legs: [tripLeg({ routed: routeLeg({ distance_m: 40_000 }) })],
  })
}

function clientReturning(...blurbs: (string | null)[]) {
  const tripBlurb = vi.fn(() =>
    Promise.resolve({ blurb: blurbs.length > 1 ? blurbs.shift() ?? null : (blurbs[0] ?? null) }),
  )
  return { tripBlurb }
}

function clientRejecting(error: Error) {
  return { tripBlurb: vi.fn(() => Promise.reject(error)) }
}

describe('useTripBlurb', () => {
  it('says nothing for a trip that does not exist yet, and asks nothing', () => {
    const client = clientReturning('never used')

    const { result } = renderHook(() => useTripBlurb(client, 'wabdr-north', null))

    expect(result.current).toBeNull()
    expect(client.tripBlurb).not.toHaveBeenCalled()
  })

  it('asks nothing about a trip with no waypoints', () => {
    // There is nothing to characterise, and the static greeting is the better line anyway:
    // it names the map path, which a rider who has placed nothing still needs.
    const client = clientReturning('never used')

    renderHook(() => useTripBlurb(client, 'wabdr-north', tripFixture({ waypoints: [] })))

    expect(client.tripBlurb).not.toHaveBeenCalled()
  })

  it('produces a line once there is a trip to describe', async () => {
    const client = clientReturning('Three days of dirt and one hot shower.')

    const { result } = renderHook(() => useTripBlurb(client, 'wabdr-north', withPoints('Ellensburg', 'Cashmere')))

    await waitFor(() => {
      expect(result.current).toBe('Three days of dirt and one hot shower.')
    })
    expect(client.tripBlurb).toHaveBeenCalledWith('wabdr-north', {}, expect.anything())
  })

  it('says nothing when the backend has nothing to say', async () => {
    // Null is the documented answer for an unusable reply, not an error. Same outcome as a
    // failure, on purpose.
    const client = clientReturning(null)
    const permissive = clientReturning('a line')

    const { result } = renderHook(() => useTripBlurb(client, 'wabdr-north', withPoints('Ellensburg')))
    // A control, because `null` is what this hook reports before it has asked as well as
    // after a null answer, and no amount of waiting tells those apart. The permissive hook
    // rendered beside it transitions, so the subject's null is a state it settled in.
    const { result: control } = renderHook(() => useTripBlurb(permissive, 'wabdr-north', withPoints('Cashmere')))

    await waitFor(() => {
      expect(control.current).toBe('a line')
    })
    expect(result.current).toBeNull()
  })

  it('says nothing when the assistant is not configured, rather than showing a 501', async () => {
    const client = clientRejecting(new ApiNotImplementedError({ detail: 'no chat model' }))
    const permissive = clientReturning('a line')

    const { result } = renderHook(() => useTripBlurb(client, 'wabdr-north', withPoints('Ellensburg')))
    const { result: control } = renderHook(() => useTripBlurb(permissive, 'wabdr-north', withPoints('Cashmere')))

    await waitFor(() => {
      expect(control.current).toBe('a line')
    })
    expect(result.current).toBeNull()
  })

  it('says nothing when the request fails outright', async () => {
    const client = clientRejecting(new ApiNetworkError({ detail: 'offline' }))
    const permissive = clientReturning('a line')

    const { result } = renderHook(() => useTripBlurb(client, 'wabdr-north', withPoints('Ellensburg')))
    const { result: control } = renderHook(() => useTripBlurb(permissive, 'wabdr-north', withPoints('Cashmere')))

    await waitFor(() => {
      expect(control.current).toBe('a line')
    })
    expect(result.current).toBeNull()
  })

  it('does not ask again for a trip whose shape has not changed', async () => {
    // The quota test. A drag that commits without changing the trip's shape re-renders this
    // hook with a new object every time, and must not spend a model call for it.
    const client = clientReturning('Three days of dirt.')
    const { result, rerender } = renderHook(({ trip }: { trip: Trip }) => useTripBlurb(client, 'wabdr-north', trip), {
      initialProps: { trip: withPoints('Ellensburg', 'Cashmere') },
    })

    await waitFor(() => {
      expect(result.current).toBe('Three days of dirt.')
    })

    for (let nudge = 1; nudge <= 5; nudge++) {
      const moved = withPoints('Ellensburg', 'Cashmere')
      rerender({
        trip: {
          ...moved,
          waypoints: moved.waypoints.map((point) => ({
            ...point,
            coordinate: { lat: 47 + nudge / 10_000, lon: -120 },
          })),
        },
      })
    }

    expect(client.tripBlurb).toHaveBeenCalledTimes(1)
    expect(result.current).toBe('Three days of dirt.')
  })

  it('asks again once the trip really is a different one', async () => {
    const client = clientReturning('Two days out of Ellensburg.', 'Now with a night in Cashmere.')
    const { result, rerender } = renderHook(({ trip }: { trip: Trip }) => useTripBlurb(client, 'wabdr-north', trip), {
      initialProps: { trip: withPoints('Ellensburg') },
    })

    await waitFor(() => {
      expect(result.current).toBe('Two days out of Ellensburg.')
    })

    rerender({ trip: withPoints('Ellensburg', 'Cashmere') })

    await waitFor(() => {
      expect(result.current).toBe('Now with a night in Cashmere.')
    })
    expect(client.tripBlurb).toHaveBeenCalledTimes(2)
  })

  it('keeps the line it already had while a new one is being fetched', async () => {
    // No flicker back to the static greeting mid-request. The old line is still true enough
    // and an empty header for a second reads as something breaking.
    let release = (): void => {}
    const client = {
      tripBlurb: vi
        .fn()
        .mockResolvedValueOnce({ blurb: 'Two days out of Ellensburg.' })
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              release = () => {
                resolve({ blurb: 'Now with a night in Cashmere.' })
              }
            }),
        ),
    }
    const { result, rerender } = renderHook(({ trip }: { trip: Trip }) => useTripBlurb(client, 'wabdr-north', trip), {
      initialProps: { trip: withPoints('Ellensburg') },
    })

    await waitFor(() => {
      expect(result.current).toBe('Two days out of Ellensburg.')
    })

    rerender({ trip: withPoints('Ellensburg', 'Cashmere') })
    expect(result.current).toBe('Two days out of Ellensburg.')

    await act(async () => {
      release()
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(result.current).toBe('Now with a night in Cashmere.')
    })
  })

  it('drops a reply that a newer trip has already superseded', async () => {
    // The stale-response rule the drag path already lives by. A slow first reply landing
    // after a fast second one would describe a trip the rider has moved on from.
    const resolvers: ((value: { blurb: string }) => void)[] = []
    const client = {
      tripBlurb: vi.fn(
        () =>
          new Promise<{ blurb: string }>((resolve) => {
            resolvers.push(resolve)
          }),
      ),
    }
    const { result, rerender } = renderHook(({ trip }: { trip: Trip }) => useTripBlurb(client, 'wabdr-north', trip), {
      initialProps: { trip: withPoints('Ellensburg') },
    })

    await waitFor(() => {
      expect(client.tripBlurb).toHaveBeenCalledTimes(1)
    })
    rerender({ trip: withPoints('Ellensburg', 'Cashmere') })
    await waitFor(() => {
      expect(client.tripBlurb).toHaveBeenCalledTimes(2)
    })

    await act(async () => {
      resolvers[1]?.({ blurb: 'the current trip' })
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(result.current).toBe('the current trip')
    })

    // The first request answers last, about a trip that is two edits old.
    await act(async () => {
      resolvers[0]?.({ blurb: 'the trip as it was' })
      await Promise.resolve()
    })
    expect(result.current).toBe('the current trip')
  })

  it('does not ask again when only the client object changed identity', async () => {
    // Written after three mutation checks showed the quota tests passing against an
    // implementation with the dedupe torn out — the effect's own dependencies were doing the
    // work, so nothing distinguished a hook that deduped from one that could not.
    //
    // This is the case that actually bites, and it has bitten this codebase before: `client`
    // is an injectable prop, and a caller constructing one inline hands this hook a new
    // object every render. Keyed on the client, that is a model call per render.
    const client = clientReturning('Three days of dirt.')
    const trip = withPoints('Ellensburg', 'Cashmere')
    const { result, rerender } = renderHook(
      ({ each }: { each: typeof client }) => useTripBlurb(each, 'wabdr-north', trip),
      { initialProps: { each: client } },
    )

    await waitFor(() => {
      expect(result.current).toBe('Three days of dirt.')
    })

    // A fresh object each time, wrapping the same call counter.
    for (let render = 0; render < 5; render++) {
      rerender({ each: { tripBlurb: client.tripBlurb } })
    }

    expect(client.tripBlurb).toHaveBeenCalledTimes(1)
    expect(result.current).toBe('Three days of dirt.')
  })

  it('takes the line down when the backend has nothing to say about the trip as it now is', async () => {
    // The other gap the mutations found: every null test started from null, so an
    // implementation that kept the previous line on a null answer passed all of them. A
    // blurb about the old trip left standing over a new one is the feature lying, which is
    // the same fault as a stale response.
    const client = {
      tripBlurb: vi
        .fn()
        .mockResolvedValueOnce({ blurb: 'Two days out of Ellensburg.' })
        .mockResolvedValueOnce({ blurb: null }),
    }
    const { result, rerender } = renderHook(({ trip }: { trip: Trip }) => useTripBlurb(client, 'wabdr-north', trip), {
      initialProps: { trip: withPoints('Ellensburg') },
    })

    await waitFor(() => {
      expect(result.current).toBe('Two days out of Ellensburg.')
    })

    rerender({ trip: withPoints('Ellensburg', 'Cashmere') })

    await waitFor(() => {
      expect(result.current).toBeNull()
    })
  })

  it('asks about the trip it was given, including its POIs', async () => {
    const client = clientReturning('a line')
    const withPoi = tripFixture({
      waypoints: [waypoint('Ellensburg')],
      legs: [tripLeg({ routed: routeLeg({ distance_m: 40_000 }) })],
      pois: [poi({ id: 'a', category: 'campground' })],
    })

    const { result } = renderHook(() => useTripBlurb(client, 'wabdr-north', withPoi))

    await waitFor(() => {
      expect(result.current).toBe('a line')
    })
    expect(client.tripBlurb).toHaveBeenCalledWith('wabdr-north', {}, expect.anything())
  })
})
