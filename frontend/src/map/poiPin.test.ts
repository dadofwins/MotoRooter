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
  function group(...categories: PoiCategory[]): Poi[] {
    return categories.map((category, index) =>
      poi({ id: `p${String(index)}`, category, name: `place ${String(index)}` }),
    )
  }

  it('says how many places are in it', () => {
    expect(createClusterPin(group('campground', 'food', 'fuel')).textContent).toBe('3')
  })

  it('names itself for a screen reader, in words rather than a bare number', () => {
    // "7" alone announces as a number with no noun. A rider using a screen reader on a map
    // needs to know it is seven places, not seven of something unnamed.
    const seven = group('campground', 'food', 'fuel', 'water', 'hotel', 'viewpoint', 'mechanic')

    expect(createClusterPin(seven).getAttribute('aria-label')).toMatch(/7 places/i)
  })

  it('is a button, because it is the only way to reach what is underneath', () => {
    // Not decoration and not an image: everything in the group is unreachable except through
    // it, so it has to be announced as something you can operate.
    expect(createClusterPin(group('campground', 'food')).getAttribute('role')).toBe('button')
  })

  it('stays legible when the group is big', () => {
    // Twelve was the largest group measured on a real corridor at the planning zoom. A pin whose
    // number overflows its own circle is worse than no number.
    const twelve = group(...(Array.from({ length: 12 }, () => 'campground') as PoiCategory[]))
    const nine = group(...(Array.from({ length: 9 }, () => 'campground') as PoiCategory[]))

    expect(createClusterPin(twelve).className).toMatch(/poi-cluster--wide/)
    expect(createClusterPin(nine).className).not.toMatch(/poi-cluster--wide/)
  })

  it('wears the colour of what is in it when that is one thing', () => {
    // Tim's call, and it overrides the neutral slate this started as: a group of campgrounds is
    // a purple thing, and saying so costs nothing.
    const pin = createClusterPin(group('campground', 'wild_camp'))

    expect(pin.style.background).toContain('--poi-stay')
    expect(pin.style.background).not.toContain('--poi-supply')
  })

  it('combines the colours when the group is mixed', () => {
    // "Say there's a purple one and an orange one being condensed, the icon will be half purple
    // and half orange."
    const pin = createClusterPin(group('campground', 'viewpoint'))

    expect(pin.style.background).toContain('conic-gradient')
    expect(pin.style.background).toContain('--poi-stay')
    expect(pin.style.background).toContain('--poi-sight')
    expect(pin.style.background).toContain('50%')
  })

  it('gives each colour the share of the group it actually has', () => {
    // Equal slices would say a group of seven campgrounds and one viewpoint is half viewpoint,
    // which is the wrong thing to tell someone deciding whether to open it.
    const pin = createClusterPin(group('campground', 'campground', 'campground', 'viewpoint'))

    expect(pin.style.background).toContain('75%')
  })

  it('slices by colour rather than by category, so no seam is invisible', () => {
    // Nine categories share three colours. A slice per category would draw two purple wedges
    // side by side and claim to have said something.
    const pin = createClusterPin(group('campground', 'wild_camp', 'hotel', 'viewpoint'))

    expect(pin.style.background.match(/var\(/g) ?? []).toHaveLength(2)
  })

  it('puts the colours in the same order every time', () => {
    // The pin is redrawn whenever the map regroups. One that reshuffles its wedges between
    // renders reads as two different places.
    const one = createClusterPin(group('viewpoint', 'campground'))
    const other = createClusterPin(group('campground', 'viewpoint'))

    expect(one.style.background).toBe(other.style.background)
  })
})
