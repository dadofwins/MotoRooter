import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { useDiscoveryCategories } from './useDiscoveryCategories'
import { DEFAULT_CATEGORIES } from './discoveryCategories'

/**
 * Remembering what a rider looks for.
 *
 * The integrator's point was that whatever is selected on the first press is what almost every
 * run will use. Persisting turns that from *my* default into *theirs* after one run — which is a
 * better default than any I could choose, and it means a rider who only ever wants camps stops
 * re-ticking four boxes every time.
 *
 * A browser preference, like the distance unit, and stored the same way: it describes this
 * browser rather than the trip, so it does not belong in the trip document.
 */
describe('useDiscoveryCategories', () => {
  it('starts on the chosen default when nothing is remembered', () => {
    const { result } = renderHook(() => useDiscoveryCategories())

    expect(result.current.categories).toEqual(DEFAULT_CATEGORIES)
  })

  it('remembers a choice for next time', () => {
    const first = renderHook(() => useDiscoveryCategories())
    act(() => {
      first.result.current.setCategories(['food', 'fuel'])
    })
    first.unmount()

    const second = renderHook(() => useDiscoveryCategories())

    expect(second.result.current.categories).toEqual(['food', 'fuel'])
  })

  it('ignores a stored value that is not a list of categories', () => {
    // Written by an older build, or edited by hand. A junk value must not take discovery down.
    localStorage.setItem('motorooter.discoveryCategories', '{"nope":true}')

    const { result } = renderHook(() => useDiscoveryCategories())

    expect(result.current.categories).toEqual(DEFAULT_CATEGORIES)
  })

  it('drops entries that are not categories the app knows', () => {
    // A category removed from the enum would otherwise be sent to the API forever.
    localStorage.setItem(
      'motorooter.discoveryCategories',
      JSON.stringify(['wild_camp', 'helipad']),
    )

    const { result } = renderHook(() => useDiscoveryCategories())

    expect(result.current.categories).toEqual(['wild_camp'])
  })

  it('falls back to the default rather than remembering an empty choice', () => {
    // Nothing selected means a run that finds nothing and still costs the route search.
    localStorage.setItem('motorooter.discoveryCategories', JSON.stringify([]))

    const { result } = renderHook(() => useDiscoveryCategories())

    expect(result.current.categories).toEqual(DEFAULT_CATEGORIES)
  })
})
