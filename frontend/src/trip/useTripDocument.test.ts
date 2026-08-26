import { StrictMode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useStoredTrip, useTripSave } from './useTripDocument'
import { ApiError } from '../api/errors'
import { trip as tripFixture, tripLeg, waypoint as waypointFixture } from '../api/fixtures'
import type { RequestOptions } from '../api/client'
import type { CreateTripRequest, Trip, UpdateTripRequest, Waypoint } from '../api/types'

/**
 * The trip document behind the map.
 *
 * Read and write are separate hooks because combining them created a cycle: the content to
 * save is derived from the document that was loaded, so one hook needed its own output as its
 * input. `useStoredTrip` depends on nothing; `useTripSave` depends on it.
 *
 * Until this existed the app held everything in component state: nothing survived a reload,
 * nothing could be shared, and three of the five MVP items were addressed by a slug that never
 * existed. This is the piece that makes a trip a document.
 *
 * Two behaviours carry the design. A trip is created without asking — a rider should not fill
 * in a name before putting two points on a map — and edits are saved on a debounce, because a
 * drag commits often and every save is a write to a bucket.
 */

function content(waypoints: readonly Waypoint[]) {
  return { waypoints, legs: [tripLeg()], pois: [] }
}

/**
 * Drives the writing half with nothing already stored, which is the common case.
 *
 * Named `useSaving` because it calls a hook: the rules-of-hooks lint keys on the name, and it
 * is right to — a helper that calls hooks is a hook, whatever it is called.
 */
function useSaving(client: ReturnType<typeof fakeClient>, waypoints: readonly Waypoint[]) {
  return useTripSave(client, content(waypoints), { slug: null, onConflict: () => undefined })
}

function fakeClient(overrides: Partial<Trip> = {}) {
  return {
    createTrip: vi.fn((request: CreateTripRequest, _options?: RequestOptions) =>
      Promise.resolve(tripFixture({ slug: request.slug ?? 'derived-slug', name: request.name, ...overrides })),
    ),
    getTrip: vi.fn((slug: string, _options?: RequestOptions) =>
      Promise.resolve(tripFixture({ slug, ...overrides })),
    ),
    updateTrip: vi.fn((slug: string, _request: UpdateTripRequest, _options?: RequestOptions) =>
      Promise.resolve(tripFixture({ slug, ...overrides })),
    ),
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  vi.useRealTimers()
})

/** Lets the debounce elapse and the resulting request settle. */
async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000)
  })
}

describe('useTripDocument', () => {
  it('creates nothing until there is something worth saving', async () => {
    const client = fakeClient()

    renderHook(() => useSaving(client, []))
    await settle()

    // An empty map is not a trip. Creating one on load would litter the bucket with blanks.
    expect(client.createTrip).not.toHaveBeenCalled()
  })

  it('creates a trip on the first waypoint, without asking for a name', async () => {
    // A rider should not have to name a trip before putting two points on a map.
    const client = fakeClient()

    const { result } = renderHook(() => useSaving(client, [waypointFixture(47)]))
    await settle()

    expect(client.createTrip).toHaveBeenCalledTimes(1)
    expect(result.current.slug).not.toBeNull()
  })

  it('reports the slug under StrictMode, which remounts every component', async () => {
    // Tim hit this in the browser: routes saved, and neither the Replan button nor the
    // "Saved" line ever appeared, because both are gated on the slug reaching the client.
    //
    // The cause was an on-screen ref cleared by an effect cleanup and never set on the way
    // in. StrictMode mounts, unmounts and remounts in development, so it latched false on
    // that first simulated unmount and stayed there — every trip was created on the server
    // and every slug was thrown away, orphaning a trip per edit.
    //
    // Rendered through StrictMode deliberately: the app runs inside it, so a test that does
    // not is testing a configuration nobody uses.
    const client = fakeClient()

    const { result } = renderHook(() => useSaving(client, [waypointFixture(47)]), {
      wrapper: StrictMode,
    })
    await settle()

    expect(client.createTrip).toHaveBeenCalledTimes(1)
    expect(result.current.slug).not.toBeNull()
  })

  it('gives the slug a random suffix, so two riders who just click do not collide', async () => {
    // Both would otherwise derive the same slug from the same default name, and the second
    // would get a 409 on their first trip.
    const client = fakeClient()

    renderHook(() => useSaving(client, [waypointFixture(47)]))
    await settle()

    const slug = client.createTrip.mock.calls[0]?.[0].slug
    expect(slug).toMatch(/-[a-z0-9]{4,}$/)
  })

  it('puts the slug in the URL, because that is the whole sharing model', async () => {
    const client = fakeClient()

    renderHook(() => useSaving(client, [waypointFixture(47)]))
    await settle()

    expect(new URL(window.location.href).searchParams.get('trip')).not.toBeNull()
  })

  it('loads the trip named in the URL, because a shared link is the sharing model', async () => {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const client = fakeClient({ waypoints: [waypointFixture(47), waypointFixture(48)] })

    const { result } = renderHook(() => useStoredTrip(client))
    await settle()

    expect(client.getTrip).toHaveBeenCalledWith('wabdr-north', expect.anything())
    expect(result.current.trip?.waypoints).toHaveLength(2)
  })

  it('reads nothing when the URL names no trip', async () => {
    const client = fakeClient()

    const { result } = renderHook(() => useStoredTrip(client))
    await settle()

    expect(client.getTrip).not.toHaveBeenCalled()
    expect(result.current.trip).toBeNull()
  })

  it('re-reads on demand, which is what a conflict asks for', async () => {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const client = fakeClient()
    const { result } = renderHook(() => useStoredTrip(client))
    await settle()

    await act(async () => {
      result.current.reload()
      await Promise.resolve()
    })

    expect(client.getTrip).toHaveBeenCalledTimes(2)
  })

  it('asks the reader to re-read on a conflict, rather than merging on its own', async () => {
    // The backend already read-merge-retries and only surfaces 409 once it has genuinely
    // lost, so merging again here would be guessing on top of an authority that has tried.
    const client = fakeClient()
    const onConflict = vi.fn()
    const { rerender } = renderHook(
      ({ points }) =>
        useTripSave(client, content(points), { slug: 'wabdr-north', onConflict }),
      { initialProps: { points: [waypointFixture(47)] as readonly Waypoint[] } },
    )
    await settle()

    client.updateTrip.mockRejectedValueOnce(
      new ApiError({ status: 409, code: 'trip_modified_concurrently', detail: 'contended' }),
    )
    rerender({ points: [waypointFixture(47), waypointFixture(48)] })
    await settle()

    expect(onConflict).toHaveBeenCalledTimes(1)
  })

  it('saves an edit once the rider stops editing, not once per change', async () => {
    // A drag commits often and every save is a write to a bucket.
    const client = fakeClient()
    const { rerender } = renderHook(({ points }) => useSaving(client, points), {
      initialProps: { points: [waypointFixture(47)] as readonly Waypoint[] },
    })
    await settle()

    // One save for the trip's creation, then three rapid edits that collapse into one more.
    expect(client.updateTrip).toHaveBeenCalledTimes(1)

    rerender({ points: [waypointFixture(47), waypointFixture(48)] })
    rerender({ points: [waypointFixture(47), waypointFixture(48), waypointFixture(49)] })
    rerender({ points: [waypointFixture(47), waypointFixture(48), waypointFixture(49.5)] })
    await settle()

    expect(client.updateTrip).toHaveBeenCalledTimes(2)
    // And it writes the newest state, not the first of the burst.
    expect(client.updateTrip.mock.calls[1]?.[1].waypoints).toHaveLength(3)
  })

  it('says when it is saving and when it has saved', async () => {
    const client = fakeClient()
    const { result, rerender } = renderHook(
      ({ points }) => useSaving(client, points),
      { initialProps: { points: [waypointFixture(47)] as readonly Waypoint[] } },
    )
    await settle()

    rerender({ points: [waypointFixture(47), waypointFixture(48)] })
    await settle()

    expect(result.current.status).toBe('saved')
  })

  it('re-reads and replaces on a conflict, rather than guessing at a merge', async () => {
    // The backend already read-merge-retries internally and only surfaces 409 once it has
    // genuinely lost, so a client-side merge would be guessing on top of an authority that
    // has already tried. The rider is told their change was replaced.
    const client = fakeClient()
    const { result, rerender } = renderHook(
      ({ points }) => useSaving(client, points),
      { initialProps: { points: [waypointFixture(47)] as readonly Waypoint[] } },
    )
    await settle() // the trip exists and its first save succeeded

    // Somebody else edits it, and the backend's own retry loses.
    client.updateTrip.mockRejectedValueOnce(
      new ApiError({ status: 409, code: 'trip_modified_concurrently', detail: 'contended' }),
    )
    rerender({ points: [waypointFixture(47), waypointFixture(48)] })
    await settle()

    expect(result.current.status).toBe('conflict')
  })

  it('reports a save that failed for any other reason', async () => {
    const client = fakeClient()
    const { result, rerender } = renderHook(
      ({ points }) => useSaving(client, points),
      { initialProps: { points: [waypointFixture(47)] as readonly Waypoint[] } },
    )
    await settle()

    client.updateTrip.mockRejectedValueOnce(
      new ApiError({ status: 503, code: 'trip_storage_unavailable', detail: 'down' }),
    )
    rerender({ points: [waypointFixture(47), waypointFixture(48)] })
    await settle()

    expect(result.current.status).toBe('failed')
    expect(result.current.error).not.toBeNull()
  })

  it('does not create a second trip for the same session', async () => {
    const client = fakeClient()
    const { rerender } = renderHook(({ points }) => useSaving(client, points), {
      initialProps: { points: [waypointFixture(47)] as readonly Waypoint[] },
    })
    await settle()

    rerender({ points: [waypointFixture(47), waypointFixture(48)] })
    await settle()

    expect(client.createTrip).toHaveBeenCalledTimes(1)
  })

  it('abandons a save in flight when unmounted', async () => {
    const signals: AbortSignal[] = []
    const client = fakeClient()
    client.updateTrip.mockImplementation((_slug, _request, options) => {
      if (options?.signal !== undefined) signals.push(options.signal)
      return new Promise(() => undefined)
    })
    const { rerender, unmount } = renderHook(
      ({ points }) => useSaving(client, points),
      { initialProps: { points: [waypointFixture(47)] as readonly Waypoint[] } },
    )
    await settle()
    rerender({ points: [waypointFixture(47), waypointFixture(48)] })
    await settle()

    unmount()

    expect(signals.at(-1)?.aborted).toBe(true)
  })
})

/**
 * Bringing a trip into existence on demand.
 *
 * The chat rail needs one before it can say anything: the endpoint is addressed by slug, and
 * the app's own opening line invites the rider to describe a trip *before* placing a point. So
 * "created on the first waypoint" is not the only trigger — it is the one the mouse uses.
 */
describe('useTripSave.ensure', () => {
  it('creates a trip when there is none, and answers with its slug', async () => {
    const client = fakeClient({ slug: 'trip-abc123' })
    const { result } = renderHook(() => useSaving(client, []))

    let slug: string | null = null
    await act(async () => {
      slug = await result.current.ensure()
    })

    expect(client.createTrip).toHaveBeenCalledTimes(1)
    expect(slug).toBe('trip-abc123')
    expect(result.current.slug).toBe('trip-abc123')
  })

  it('answers with the trip that already exists rather than making another', async () => {
    const client = fakeClient()
    const { result } = renderHook(() =>
      useTripSave(client, content([]), { slug: 'wabdr-north', onConflict: () => undefined }),
    )

    let slug: string | null = null
    await act(async () => {
      slug = await result.current.ensure()
    })

    expect(slug).toBe('wabdr-north')
    expect(client.createTrip).not.toHaveBeenCalled()
  })

  it('makes one trip when two things ask at once', async () => {
    // The rider sends a chat message and places a point in the same breath. Two creations
    // means two documents and the second silently orphans the first.
    const client = fakeClient({ slug: 'trip-abc123' })
    const { result } = renderHook(() => useSaving(client, []))

    await act(async () => {
      await Promise.all([result.current.ensure(), result.current.ensure()])
    })

    expect(client.createTrip).toHaveBeenCalledTimes(1)
  })

  it('carries the name the rider typed at the front door', async () => {
    const client = fakeClient()
    const { result } = renderHook(() =>
      useTripSave(client, content([]), {
        slug: null,
        name: 'Cascades loop',
        onConflict: () => undefined,
      }),
    )

    await act(async () => {
      await result.current.ensure()
    })

    expect(client.createTrip.mock.calls[0]?.[0].name).toBe('Cascades loop')
  })

  it('does not name a trip in the URL after the rider has left it', async () => {
    // The sequence: creation in flight, rider clicks New trip, the URL is cleared and the
    // session remounts — then the old creation resolves and writes the slug back. The app shows
    // the front door while the URL names a trip, so a reload lands them back in the trip they
    // just left. Narrow window, no data loss, and confusing enough to be worth closing.
    let release: ((trip: Trip) => void) | null = null
    const client = fakeClient()
    client.createTrip.mockImplementation(
      () =>
        new Promise<Trip>((resolve) => {
          release = resolve
        }),
    )
    const view = renderHook(() => useSaving(client, []))
    void view.result.current.ensure()
    await act(async () => {
      await Promise.resolve()
    })

    view.unmount()
    await act(async () => {
      release?.(tripFixture({ slug: 'trip-orphaned' }))
      await Promise.resolve()
    })

    expect(new URL(window.location.href).searchParams.get('trip')).toBeNull()
  })

  it('lets a second attempt through after a failure', async () => {
    // A failed creation is not an answer to be remembered, or the rail is dead for the
    // session over one dropped request.
    const client = fakeClient()
    client.createTrip.mockRejectedValueOnce(new Error('network'))
    const { result } = renderHook(() => useSaving(client, []))

    await act(async () => {
      await expect(result.current.ensure()).rejects.toThrow()
    })
    await act(async () => {
      await result.current.ensure()
    })

    expect(client.createTrip).toHaveBeenCalledTimes(2)
  })
})
