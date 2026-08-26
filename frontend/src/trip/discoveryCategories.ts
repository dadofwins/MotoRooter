/**
 * Which kinds of place a discovery run looks for.
 *
 * The mouse equivalent of `find_places`' `categories` argument — the third and last gap where
 * the assistant could say something the mouse could not. "Find me more restaurants" worked by
 * typing and had no equivalent by clicking.
 *
 * It is also a cost control, and that is not a side effect. Discovery fans out one metered
 * search per anchor **per category**, so nine categories over a long corridor is most of the
 * spend, and a rider narrowing to "somewhere to sleep" should make the run genuinely cheaper and
 * faster rather than merely tidier.
 *
 * **The default is the decision here, not the control.** Whatever is selected the first time a
 * rider presses the button is what almost every run will use, so it is chosen rather than
 * inherited from what the API happens to accept.
 */
import type { PoiCategory } from '../api/types'
import type { PoiGroup } from '../map/poiPin'

/**
 * What a first run looks for: everywhere you could sleep, and the things worth stopping for.
 *
 * Reasoning, so the next person can disagree with the argument rather than the list. Where to
 * sleep is the part of a trip a rider *must* plan ahead — it is the decision that constrains the
 * route, and getting it wrong at 8pm on a forest road is the failure this app exists to avoid.
 * Viewpoints are the other half of why the trip is being taken at all.
 *
 * Food, fuel, water and mechanics are deliberately off. They are the noisiest categories and the
 * most expensive to search — a fuel station every 25 km is not information — and they are things
 * a rider looks up when they need one rather than plans a route around. One tick turns them on
 * for the rider who wants them.
 */
export const DEFAULT_CATEGORIES: readonly PoiCategory[] = [
  'wild_camp',
  'campground',
  'hotel',
  'unique_stay',
  'viewpoint',
]

/**
 * The categories in the order they are offered, under the groups the rest of the app uses.
 *
 * Grouped for the eye only: the request still carries categories, so "just restaurants" is
 * expressible. Selecting by group would have closed the gap narrowly enough to leave the
 * assistant able to ask for something the mouse still could not.
 */
export const CATEGORY_GROUPS: readonly { group: PoiGroup; title: string; categories: readonly PoiCategory[] }[] = [
  { group: 'stay', title: 'Stays', categories: ['wild_camp', 'campground', 'hotel', 'unique_stay'] },
  { group: 'supply', title: 'Supplies', categories: ['food', 'fuel', 'water', 'mechanic'] },
  { group: 'sight', title: 'Sights', categories: ['viewpoint'] },
]

/** Every category the app knows, in the offered order. */
export const ALL_CATEGORIES: readonly PoiCategory[] = CATEGORY_GROUPS.flatMap(
  (entry) => entry.categories,
)
