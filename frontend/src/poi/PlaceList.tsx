/**
 * The discovered places, as a list.
 *
 * Built because of what Tim said after a *successful* discovery run — "I don't see any to click
 * on" — of twenty-nine places that had been found, resolved and judged. They were pins, and he
 * was right anyway: twenty-nine pins on a map is a haystack, and the rail is where a rider
 * decides.
 *
 * The job is triage, not detail. Enough to tell one place from another **without a Places call**,
 * because Google's terms mean anything richer has to be re-fetched every time it is shown, and
 * paying for twenty-nine of those to draw a list would be absurd. So a row carries what the
 * trip document already holds: what kind of place it is, whether it is on the route, whether it
 * was ever confirmed, and the discovery judge's own note about why it was kept.
 *
 * Grouped by the same three groups the pins use, so the list and the map agree about what a
 * thing is — a rider matching one to the other should not have to do a puzzle.
 */
import { isVerified, poiGroup, poiLabel, type PoiGroup } from '../map/poiPin'
import { PoiMark } from './PoiMark'
import type { Poi } from '../api/types'

export interface PlaceListProps {
  readonly pois: readonly Poi[]
  readonly onOpen: (poi: Poi) => void
  /** Take it off the trip. Durable, because discovered places are saved into the document. */
  readonly onIgnore: (poi: Poi) => void
  /**
   * Route through a whole group at once.
   *
   * Per group rather than one button for everything found. Tim asked for "a button to route
   * through found POIs", and twenty-nine places is not an itinerary — it is a search result, and
   * a control nobody presses is the demo-shaped version of this feature. A group is how a rider
   * thinks about it: these are where I sleep, those are what I want to see, and I do not want a
   * fuel station as a waypoint. Omit the prop and no bulk action is offered.
   */
  readonly onRouteThrough?: (pois: readonly Poi[]) => void
}

const GROUP_ORDER: readonly { group: PoiGroup; title: string; one: string; many: string }[] = [
  { group: 'stay', title: 'Stays', one: 'stay', many: 'stays' },
  { group: 'supply', title: 'Supplies', one: 'supply', many: 'supplies' },
  { group: 'sight', title: 'Sights', one: 'sight', many: 'sights' },
]

/**
 * Places a bulk route-through would actually add.
 *
 * Anything already on the route is done, and an unconfirmed suggestion cannot be pinned at all —
 * the backend refuses it. Counting either would put a number in the button that the action would
 * not deliver, and the count is the whole point of the label.
 */
function routable(pois: readonly Poi[]): readonly Poi[] {
  return pois.filter((poi) => poi.on_route !== true && isVerified(poi))
}

export function PlaceList({
  pois,
  onOpen,
  onIgnore,
  onRouteThrough,
}: PlaceListProps): React.JSX.Element | null {
  // Nothing rather than an empty panel: a heading over no rows reads as broken rather than as a
  // discovery run nobody has made yet.
  if (pois.length === 0) return null

  return (
    <section className="places" aria-label="Places found">
      <h2 className="places__title">
        Places · <span className="places__count">{pois.length} places</span>
      </h2>

      {GROUP_ORDER.map(({ group, title, one, many }) => {
        // Best-judged first. `Poi.score` is the judge's ranking key and this is what it is good
        // for here: showing it as a number would be meaningless without a scale, but ordering by
        // it means someone scanning twenty-nine places meets the good ones first — the same use
        // the backend makes of it when it caps a list. A place with no score keeps its position
        // rather than sinking as though it scored zero, because an unjudged place is usually one
        // the rider added themselves.
        const inGroup = pois
          .filter((poi) => poiGroup(poi.category) === group)
          .map((poi, order) => ({ poi, order }))
          .sort((a, b) => {
            const scored = (b.poi.score ?? -1) - (a.poi.score ?? -1)
            return a.poi.score === undefined || a.poi.score === null || b.poi.score === undefined || b.poi.score === null
              ? a.order - b.order
              : scored || a.order - b.order
          })
          .map((entry) => entry.poi)
        // A heading over nothing is worse than a missing heading: it reads as a failed search
        // for that kind of place rather than as one that was never made.
        if (inGroup.length === 0) return null

        const addable = routable(inGroup)

        return (
          <div key={group} className="places__group">
            <div className="places__group-head">
              <h3 className="places__group-title">{title}</h3>
              {onRouteThrough !== undefined && addable.length > 0 && (
                // The count is in the label so the commitment is visible before the click, and
                // the button is absent rather than disabled when it would add nothing — a
                // control that does nothing reads as an action that failed.
                <button
                  type="button"
                  className="places__route-through"
                  onClick={() => onRouteThrough(addable)}
                >
                  {`Route through ${String(addable.length)} ${addable.length === 1 ? one : many}`}
                </button>
              )}
            </div>
            <ul className="places__list">
              {inGroup.map((poi) => (
                <li key={poi.id} className="places__row">
                  <button
                    type="button"
                    className="places__open"
                    onClick={() => onOpen(poi)}
                  >
                    {/* The pin itself, shrunk. A row and a pin should be recognisably the same
                        object without reading either — the rail and the map are looked at
                        together, thirty places at a time. */}
                    <PoiMark category={poi.category} />
                    <span className="places__body">
                      <span className="places__name">{poi.name}</span>
                      <span className="places__meta">
                        {poiLabel(poi.category)}
                        {poi.on_route === true && ' · on the route'}
                        {/* Said here rather than discovered as a missing button in the dialog:
                            an unresolved suggestion cannot be pinned to a route at all. */}
                        {!isVerified(poi) && ' · unconfirmed'}
                      </span>
                      {poi.note !== null && poi.note !== undefined && poi.note !== '' && (
                        // The judge's own reason for keeping it, and the only real signal that
                        // costs nothing to show.
                        <span className="places__note">{poi.note}</span>
                      )}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="places__ignore"
                    // Named, because a column of bare crosses is unusable with a screen reader
                    // and ambiguous with a mouse once the list is twenty-nine long.
                    aria-label={`Ignore ${poi.name}`}
                    onClick={() => onIgnore(poi)}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )
      })}
    </section>
  )
}
