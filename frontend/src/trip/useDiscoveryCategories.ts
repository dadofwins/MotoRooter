/**
 * What this browser looks for when it runs discovery.
 *
 * The default is the decision that matters — whatever is selected on the first press is what
 * almost every run will use — and persisting turns that from *my* default into the rider's after
 * one run, which is a better default than any I could choose. A rider who only ever wants camps
 * stops re-ticking four boxes every time.
 *
 * A browser preference, like the distance unit, and stored the same way: it describes this
 * browser rather than the trip, so it has no business in the trip document.
 *
 * Storage is treated as something that may refuse and may lie. Safari in private browsing throws
 * on write, and anything at all could be under the key — including a shape written by an older
 * build, or a category that no longer exists in the enum, which would otherwise be sent to the
 * API forever.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { ALL_CATEGORIES, DEFAULT_CATEGORIES } from './discoveryCategories'
import type { PoiCategory } from '../api/types'

const STORAGE_KEY = 'motorooter.discoveryCategories'

function isCategory(value: unknown): value is PoiCategory {
  return typeof value === 'string' && ALL_CATEGORIES.some((each) => each === value)
}

function readStored(): readonly PoiCategory[] {
  try {
    const raw: unknown = localStorage.getItem(STORAGE_KEY)
    if (typeof raw !== 'string') return DEFAULT_CATEGORIES
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return DEFAULT_CATEGORIES
    const known = parsed.filter(isCategory)
    // An empty choice is a run that finds nothing and still pays for the route search, so it is
    // not something worth remembering.
    return known.length === 0 ? DEFAULT_CATEGORIES : ALL_CATEGORIES.filter((each) => known.includes(each))
  } catch {
    return DEFAULT_CATEGORIES
  }
}

export interface DiscoveryCategories {
  readonly categories: readonly PoiCategory[]
  readonly setCategories: (next: readonly PoiCategory[]) => void
}

export function useDiscoveryCategories(): DiscoveryCategories {
  const [categories, setStored] = useState<readonly PoiCategory[]>(readStored)

  // Written where a side effect belongs rather than inside the setter: React may invoke an
  // updater twice under StrictMode, and an updater is meant to be pure.
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(categories))
    } catch {
      // Unwritable storage costs the rider this preference next visit and nothing else.
    }
  }, [categories])

  const setCategories = useCallback((next: readonly PoiCategory[]) => {
    setStored(next)
  }, [])

  return useMemo(() => ({ categories, setCategories }), [categories, setCategories])
}
