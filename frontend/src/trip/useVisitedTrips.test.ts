import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useVisitedTrips } from './useVisitedTrips'

/**
 * The trips this browser has seen.
 *
 * Not ownership. Trips have no accounts and are addressed by slug, so anyone with a link has
 * full access — that is the prototype's design, not a gap this list papers over. It is a
 * convenience so a rider can find their way back, and it lives in localStorage because it
 * describes a browser rather than a trip.
 *
 * The name is stored alongside the slug so the list reads without fetching every trip, which
 * on a bucket-backed store would be one request per row.
 */

describe('useVisitedTrips', () => {
  it('starts empty', () => {
    const { result } = renderHook(() => useVisitedTrips())

    expect(result.current.trips).toEqual([])
  })

  it('remembers a trip by slug and name', () => {
    const { result } = renderHook(() => useVisitedTrips())

    act(() => {
      result.current.remember({ slug: 'wabdr-north', name: 'WABDR North' })
    })

    expect(result.current.trips).toEqual([{ slug: 'wabdr-north', name: 'WABDR North' }])
  })

  it('survives a reload, which is the entire point of it', () => {
    const first = renderHook(() => useVisitedTrips())
    act(() => {
      first.result.current.remember({ slug: 'wabdr-north', name: 'WABDR North' })
    })
    first.unmount()

    const second = renderHook(() => useVisitedTrips())

    expect(second.result.current.trips).toHaveLength(1)
  })

  it('puts the most recent first, because that is the one being worked on', () => {
    const { result } = renderHook(() => useVisitedTrips())

    act(() => {
      result.current.remember({ slug: 'older', name: 'Older' })
    })
    act(() => {
      result.current.remember({ slug: 'newer', name: 'Newer' })
    })

    expect(result.current.trips.map((trip) => trip.slug)).toEqual(['newer', 'older'])
  })

  it('updates a name rather than listing the trip twice', () => {
    // Renaming a trip should not produce two rows for one document.
    const { result } = renderHook(() => useVisitedTrips())

    act(() => {
      result.current.remember({ slug: 'wabdr-north', name: 'New trip' })
    })
    act(() => {
      result.current.remember({ slug: 'wabdr-north', name: 'WABDR North' })
    })

    expect(result.current.trips).toEqual([{ slug: 'wabdr-north', name: 'WABDR North' }])
  })

  it('forgets a trip on request, without deleting it for anyone else', () => {
    // Removing it from this list is not deletion: the link still works, which is worth the
    // UI being clear about.
    const { result } = renderHook(() => useVisitedTrips())
    act(() => {
      result.current.remember({ slug: 'wabdr-north', name: 'WABDR North' })
    })

    act(() => {
      result.current.forget('wabdr-north')
    })

    expect(result.current.trips).toEqual([])
  })

  it('keeps the list to a sane length', () => {
    const { result } = renderHook(() => useVisitedTrips())

    act(() => {
      for (let index = 0; index < 40; index++) {
        result.current.remember({ slug: `trip-${String(index)}`, name: `Trip ${String(index)}` })
      }
    })

    // A registry, not an archive. The oldest fall off rather than growing without bound.
    expect(result.current.trips.length).toBeLessThanOrEqual(20)
    expect(result.current.trips[0]?.slug).toBe('trip-39')
  })

  it('ignores stored junk rather than failing to render', () => {
    // Anyone can put anything in localStorage, including an older version of this app.
    localStorage.setItem('motorooter.visitedTrips', '{"not":"an array"}')

    const { result } = renderHook(() => useVisitedTrips())

    expect(result.current.trips).toEqual([])
  })

  it('drops entries that are not shaped like trips', () => {
    localStorage.setItem(
      'motorooter.visitedTrips',
      JSON.stringify([{ slug: 'good', name: 'Good' }, { slug: 42 }, null, { name: 'no slug' }]),
    )

    const { result } = renderHook(() => useVisitedTrips())

    expect(result.current.trips).toEqual([{ slug: 'good', name: 'Good' }])
  })

  it('works when storage refuses, which private browsing does', () => {
    const setItem = vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    const { result } = renderHook(() => useVisitedTrips())

    act(() => {
      result.current.remember({ slug: 'wabdr-north', name: 'WABDR North' })
    })

    // Still listed for this session; a list that cannot be saved is not a reason to fail.
    expect(result.current.trips).toHaveLength(1)
    setItem.mockRestore()
  })
})
