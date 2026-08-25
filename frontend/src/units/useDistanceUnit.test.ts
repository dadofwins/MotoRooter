import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useDistanceUnit } from './useDistanceUnit'

/**
 * The rider's unit preference.
 *
 * A preference, not trip state: it belongs to the person and their browser, not to a trip
 * that is world-readable by a shareable link. So `localStorage`, not the trip document — a
 * rider in Washington opening someone else's trip should still see miles.
 */


describe('useDistanceUnit', () => {
  it('starts in miles, which is where the first riders are', () => {
    const { result } = renderHook(() => useDistanceUnit())

    expect(result.current.unit).toBe('mi')
  })

  it('remembers a choice across a reload', () => {
    const first = renderHook(() => useDistanceUnit())
    act(() => {
      first.result.current.setUnit('km')
    })
    first.unmount()

    // A fresh mount is what a page reload looks like from here.
    const second = renderHook(() => useDistanceUnit())

    expect(second.result.current.unit).toBe('km')
  })

  it('ignores a stored value that is not a unit', () => {
    // Anyone can put anything in localStorage, including an older version of this app.
    localStorage.setItem('motorooter.distanceUnit', 'furlongs')

    const { result } = renderHook(() => useDistanceUnit())

    expect(result.current.unit).toBe('mi')
  })

  it('works when storage refuses to answer', () => {
    // Safari in private browsing throws on setItem, and a map that will not load because a
    // preference could not be saved is a bad trade.
    // Spied on the object rather than on Storage.prototype: this environment has no Storage
    // constructor at all, which is itself why test-setup supplies the shim.
    const getItem = vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    const setItem = vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })

    const { result } = renderHook(() => useDistanceUnit())
    expect(result.current.unit).toBe('mi')
    act(() => {
      result.current.setUnit('km')
    })
    expect(result.current.unit).toBe('km') // still switches for this session

    getItem.mockRestore()
    setItem.mockRestore()
  })

  it('keeps a stable identity so it can be depended on', () => {
    // Anything keyed on this must not be rebuilt every render — the lesson from a drag
    // session that destroyed itself because its dependency was a fresh object each time.
    const { result, rerender } = renderHook(() => useDistanceUnit())
    const first = result.current

    rerender()

    expect(result.current).toBe(first)
  })
})
