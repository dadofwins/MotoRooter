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

/**
 * The glyph for a category, for anywhere that is not a map pin.
 *
 * Exported rather than duplicated: the list in the rail and the pin on the map have to agree
 * about what a thing looks like, or a rider matching one to the other is doing a puzzle.
 */
export function poiGlyph(category: PoiCategory): string {
  return GLYPHS[category]
}

/** The rider-facing name for a category. One table, so the dialog and the list cannot drift. */
export function poiLabel(category: PoiCategory): string {
  return LABELS[category]
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

/**
 * The order colours appear in a mixed group's pin.
 *
 * Fixed, because the pin is redrawn every time the map regroups and one that reshuffles its
 * wedges between renders reads as a different place.
 */
const GROUP_ORDER: readonly PoiGroup[] = ['stay', 'supply', 'sight']

/**
 * The pin that stands for several places at once.
 *
 * Its one job the individual pins cannot do is say **how many**. Measured on a live corridor,
 * 29 of 31 pins were obscured by another at the zoom a rider plans at, and the one drawn on top
 * was whichever happened to be last — so without a number the map quietly under-reports what
 * discovery found.
 *
 * **It wears the colours of what is inside it**, in proportion. Tim's call, and it corrects a
 * decision this started with: a neutral pin said "several things, unspecified" when it could say
 * "mostly places to sleep, and one view". The wedges are per *colour*, not per category — nine
 * categories share three group colours, so a slice per category would draw two purple wedges
 * side by side and claim to have said something.
 *
 * Colours come from CSS custom properties rather than literals here, so the group colours have
 * one definition and a pin cannot drift from the map's own palette.
 */
export function createClusterPin(members: readonly Poi[]): HTMLElement {
  const count = members.length
  const pin = document.createElement('div')
  // Three digits do not fit in a circle sized for a glyph. Twelve was the largest group measured
  // on a real corridor, so this is a real case rather than a defensive one.
  pin.className = `poi-cluster${count > 9 ? ' poi-cluster--wide' : ''}`
  pin.textContent = String(count)
  pin.style.background = clusterBackground(members)

  const name = `${String(count)} places here`
  // A button, not an image: everything underneath is unreachable except through it.
  pin.setAttribute('role', 'button')
  pin.setAttribute('aria-label', name)
  pin.title = name
  return pin
}

/** One colour where the group is of one kind, wedges in proportion where it is mixed. */
function clusterBackground(members: readonly Poi[]): string {
  const shares = GROUP_ORDER.map((group) => ({
    group,
    share: members.filter((member) => poiGroup(member.category) === group).length / members.length,
  })).filter((each) => each.share > 0)

  const only = shares[0]
  if (only === undefined) return ''
  if (shares.length === 1) return `var(--poi-${only.group})`

  let sweep = 0
  const stops = shares.map(({ group, share }) => {
    const from = sweep * 100
    sweep += share
    // Rounded, so the arithmetic that produced them is not written across the pin in decimals.
    return `var(--poi-${group}) ${String(Math.round(from))}% ${String(Math.round(sweep * 100))}%`
  })
  return `conic-gradient(${stops.join(', ')})`
}
