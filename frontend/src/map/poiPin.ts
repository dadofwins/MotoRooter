/**
 * Point-of-interest pins.
 *
 * Nine categories is enough to turn a map into confetti, so meaning is carried on three axes
 * instead of one: a **shape** per group, a **glyph** per category, and colour last. A rider
 * glancing at a phone in sunlight reads shape well before hue, and a colour-blind rider may
 * not read hue at all — the same argument that made unpaved road dashed rather than orange.
 *
 * The groups are what a rider is actually looking for at a given moment: somewhere to sleep,
 * something they need, or a reason to stop.
 */
import type { Poi, PoiCategory } from '../api/types'

/**
 * Every category, in the contract's own order.
 *
 * Typed as `PoiCategory[]` in the tests, so a category renamed or removed backend-side fails
 * the build here rather than quietly leaving a pin with no icon.
 */
export const POI_CATEGORIES = [
  'wild_camp',
  'campground',
  'hotel',
  'unique_stay',
  'food',
  'fuel',
  'water',
  'viewpoint',
  'mechanic',
] as const satisfies readonly PoiCategory[]

/** Somewhere to sleep, something you need, or a reason to stop. */
export type PoiGroup = 'stay' | 'supply' | 'sight'

const GROUPS: Record<PoiCategory, PoiGroup> = {
  wild_camp: 'stay',
  campground: 'stay',
  hotel: 'stay',
  unique_stay: 'stay',
  food: 'supply',
  fuel: 'supply',
  water: 'supply',
  mechanic: 'supply',
  viewpoint: 'sight',
}

/** One per category, distinct at a glance and monochrome so the colour still means group. */
const GLYPHS: Record<PoiCategory, string> = {
  wild_camp: '▲',
  campground: '⌂',
  hotel: '▤',
  unique_stay: '★',
  food: '❖',
  fuel: '⬢',
  water: '≈',
  viewpoint: '◉',
  mechanic: '✜',
}

const LABELS: Record<PoiCategory, string> = {
  wild_camp: 'Wild camp',
  campground: 'Campground',
  hotel: 'Hotel',
  unique_stay: 'Unique stay',
  food: 'Food',
  fuel: 'Fuel',
  water: 'Water',
  viewpoint: 'Viewpoint',
  mechanic: 'Mechanic',
}

export function poiGroup(category: PoiCategory): PoiGroup {
  return GROUPS[category]
}

/**
 * Whether a POI describes a place that is known to exist.
 *
 * Mirrors the backend rule exactly: anything but an LLM suggestion that never resolved to a
 * `place_id`. The model refuses to put an unverified POI on a route, so the UI must not
 * offer to — and must not draw it as though it were confirmed either.
 */
export function isVerified(poi: Poi): boolean {
  return poi.source !== 'llm_suggested' || (poi.place_id ?? null) !== null
}

export function createPoiPin(poi: Poi): HTMLElement {
  const verified = isVerified(poi)
  const kind = LABELS[poi.category]

  const pin = document.createElement('div')
  pin.className = `poi poi--${poiGroup(poi.category)}${verified ? '' : ' poi--unverified'}`
  pin.textContent = GLYPHS[poi.category]

  // "unconfirmed" rather than a bare category: a suggestion nobody has checked is a
  // materially different thing to a rider deciding where to sleep.
  const name = verified ? `${kind}: ${poi.name}` : `${kind}, unconfirmed: ${poi.name}`
  pin.setAttribute('role', 'img')
  pin.setAttribute('aria-label', name)
  pin.title = name
  return pin
}
