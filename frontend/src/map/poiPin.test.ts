import { screen } from '@testing-library/dom'
import { describe, expect, it } from 'vitest'
import { createPoiPin, isVerified, poiGroup, POI_CATEGORIES } from './poiPin'
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
