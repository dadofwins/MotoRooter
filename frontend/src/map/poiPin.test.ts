import { screen } from '@testing-library/dom'
import { describe, expect, it } from 'vitest'
import { createClusterPin, createPoiPin, isVerified, poiGroup, POI_CATEGORIES } from './poiPin'
import type { Poi, PoiCategory } from '../api/types'

/**
 * Point-of-interest pins.
 *
 * Nine categories is enough to turn a map into confetti, so the meaning is carried on three
 * axes rather than one: a shape per *group* (somewhere to sleep, something you need, a
 * reason to stop), a glyph per category, and colour last. A rider glancing at a phone in
 * sunlight reads shape long before hue, and a colour-blind rider may not read hue at all.
 *
 * The other job here is honesty about provenance. An LLM-suggested place with no resolved
 * `place_id` is a claim, not a place — the backend refuses to pin one to a route, so the map
 * must not present it as if it were real.
 */

function poi(overrides: Partial<Poi> = {}): Poi {
  return {
    id: 'poi-1',
    name: 'Sun Mountain Lodge',
    category: 'hotel',
    coordinate: { lat: 48.5, lon: -120.2 },
    source: 'places',
    place_id: 'ChIJ123',
    note: null,
    on_route: false,
    ...overrides,
  }
}

describe('poiGroup', () => {
  it('covers every category the contract defines', () => {
    // If the backend adds a tenth category this fails to compile, rather than rendering it
    // as an unlabelled dot nobody notices.
    for (const category of POI_CATEGORIES) {
      expect(poiGroup(category)).toBeDefined()
    }
    expect(POI_CATEGORIES).toHaveLength(9)
  })

  it('groups by what a rider is looking for, not alphabetically', () => {
    expect(poiGroup('wild_camp')).toBe('stay')
    expect(poiGroup('campground')).toBe('stay')
    expect(poiGroup('hotel')).toBe('stay')
    expect(poiGroup('unique_stay')).toBe('stay')

    expect(poiGroup('food')).toBe('supply')
    expect(poiGroup('fuel')).toBe('supply')
    expect(poiGroup('water')).toBe('supply')
    expect(poiGroup('mechanic')).toBe('supply')

    expect(poiGroup('viewpoint')).toBe('sight')
  })
})

describe('isVerified', () => {
  it('accepts anything that is not an unresolved LLM suggestion', () => {
    expect(isVerified(poi({ source: 'places' }))).toBe(true)
    expect(isVerified(poi({ source: 'user', place_id: null }))).toBe(true)
    expect(isVerified(poi({ source: 'llm_suggested', place_id: 'ChIJ123' }))).toBe(true)
  })

  it('rejects an LLM suggestion that never resolved to a real place', () => {
    // The model this mirrors refuses to put one of these on the route at all.
    expect(isVerified(poi({ source: 'llm_suggested', place_id: null }))).toBe(false)
  })
})

describe('createPoiPin', () => {
  it('gives every category its own glyph', () => {
    const glyphs = POI_CATEGORIES.map((category) => createPoiPin(poi({ category })).textContent)

    expect(new Set(glyphs).size).toBe(POI_CATEGORIES.length)
  })

  it('shapes the pin by group, so colour is never the only difference', () => {
    const stay = createPoiPin(poi({ category: 'campground' }))
    const supply = createPoiPin(poi({ category: 'fuel' }))
    const sight = createPoiPin(poi({ category: 'viewpoint' }))

    expect(stay.className).toContain('poi--stay')
    expect(supply.className).toContain('poi--supply')
    expect(sight.className).toContain('poi--sight')
  })

  it('names itself for a screen reader, with the place and what it is', () => {
    document.body.append(createPoiPin(poi({ name: 'Lone Fir Campground', category: 'campground' })))

    expect(screen.getByRole('img', { name: 'Campground: Lone Fir Campground' })).toBeInTheDocument()
  })

  it('marks an unverified suggestion, visibly and in its name', () => {
    // Silently rendering it like any other pin would present a guess as a place.
    const unverified = createPoiPin(poi({ source: 'llm_suggested', place_id: null }))
    document.body.append(unverified)

    expect(unverified.className).toContain('poi--unverified')
    expect(screen.getByRole('img', { name: /unconfirmed/i })).toBeInTheDocument()
  })

  it('does not mark a resolved place as unverified', () => {
    expect(createPoiPin(poi()).className).not.toContain('poi--unverified')
  })

  it('carries a hover title as well, for a mouse user with no screen reader', () => {
    expect(createPoiPin(poi({ name: 'Rainy Pass' })).title).toContain('Rainy Pass')
  })
})

describe('the category list', () => {
  it('is exactly the contract enum, so a rename cannot pass unnoticed', () => {
    // Assigning to PoiCategory[] means the day the backend renames one, this file fails
    // rather than the map quietly losing an icon.
    const asContract: PoiCategory[] = [...POI_CATEGORIES]

    expect(asContract).toContain('wild_camp')
  })
})

/**
 * The pin that stands for several places.
 *
 * It has one job the individual pins cannot do: say how many are underneath. Measured on a live
 * corridor, 29 of 31 pins were obscured at the zoom a rider plans at — so without a number, the
 * map is silently lying about how much it found.
 */
describe('createClusterPin', () => {
  it('says how many places are in it', () => {
    expect(createClusterPin(7).textContent).toBe('7')
  })

  it('names itself for a screen reader, in words rather than a bare number', () => {
    // "7" alone announces as a number with no noun. A rider using a screen reader on a map
    // needs to know it is seven places, not seven of something unnamed.
    expect(createClusterPin(7).getAttribute('aria-label')).toMatch(/7 places/i)
  })

  it('says "places" plurally only when it means it', () => {
    // A cluster of two is the common case and the smallest one that exists; a cluster of one is
    // drawn as the place itself, so this never has to say "1 places".
    expect(createClusterPin(2).getAttribute('aria-label')).toMatch(/2 places/i)
  })

  it('is a button, because it is the only way to reach what is underneath', () => {
    // Not decoration and not an image: everything in the cluster is unreachable except through
    // it, so it has to be announced as something you can operate.
    expect(createClusterPin(3).getAttribute('role')).toBe('button')
  })

  it('stays legible when the group is big', () => {
    // Twelve was the largest group measured on a real corridor at the planning zoom. A pin whose
    // number overflows its own circle is worse than no number.
    expect(createClusterPin(12).className).toMatch(/poi-cluster--wide/)
    expect(createClusterPin(9).className).not.toMatch(/poi-cluster--wide/)
  })
})
