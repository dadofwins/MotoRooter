/**
 * Adding a point by typing its name.
 *
 * This closes the half of the original trip-creation spec that never shipped — "type a starting
 * and ending address or choose to click on the map" — because there was no geocoding until now.
 * It predates tonight's feedback rather than answering it.
 *
 * **Ambiguity is a refusal, not a best guess.** The endpoint returns a list and chooses nothing,
 * and neither does this, even when there is exactly one result. One result is still a claim, and
 * taking it silently is the shortcut that is wrong the first time a single match is the wrong
 * place. Backend's first live run hit the ambiguous case immediately: "Stevens Pass" matched two,
 * and the address was the only thing telling them apart — which is why that field exists.
 *
 * Nothing here is stored. `place_id` is the only field the terms allow keeping, and the waypoint
 * this produces carries the name rather than the identifier, because the trip document has
 * nowhere to put one.
 */
import { useCallback, useRef, useState } from 'react'
import type { ApiClient } from '../api/client'
import { isNotImplemented } from '../api/errors'
import { routeErrorMessage } from './routeErrorMessage'
import type { Coordinate, GeocodeResult } from '../api/types'

export type PlaceFinder = Pick<ApiClient, 'geocode'>

/**
 * What kind of place a result is, in a word.
 *
 * From Places' own types, which the endpoint passes through for this. The address is what makes
 * two same-named places choosable; this is what makes a list of five scannable without reading
 * every address.
 *
 * **The list is Google's and it grows**, so anything unrecognised is "Place" rather than a guess
 * or a blank. First match wins, because a result carries several types ordered specific-first.
 */
const KINDS: readonly [string, string][] = [
  ['locality', 'Town'],
  ['administrative_area_level_1', 'Region'],
  ['administrative_area_level_2', 'County'],
  ['natural_feature', 'Landmark'],
  ['park', 'Park'],
  ['campground', 'Campground'],
  ['route', 'Road'],
  ['intersection', 'Junction'],
  ['airport', 'Airport'],
]

function kindOf(kinds: readonly string[] | undefined): string {
  for (const [type, label] of KINDS) {
    if (kinds?.includes(type) === true) return label
  }
  return 'Place'
}

export interface PlaceSearchProps {
  readonly client: PlaceFinder
  /**
   * Where to bias results toward — the last point of the trip, or null when there is none.
   *
   * What makes "Leavenworth" the Washington one on a trip already in Washington. Null rather than
   * a made-up centre: inventing one would silently prefer a real place over another real place.
   */
  readonly near: Coordinate | null
  readonly onChoose: (place: GeocodeResult) => void
}

type State =
  | { readonly kind: 'idle' }
  | { readonly kind: 'searching' }
  | { readonly kind: 'results'; readonly results: readonly GeocodeResult[] }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'failed'; readonly error: unknown }

export function PlaceSearch({ client, near, onChoose }: PlaceSearchProps): React.JSX.Element {
  const [query, setQuery] = useState('')
  const [state, setState] = useState<State>({ kind: 'idle' })
  const running = useRef(false)

  const search = useCallback(() => {
    const text = query.trim()
    if (running.current || text === '') return
    running.current = true
    setState({ kind: 'searching' })

    client.geocode(text, near === null ? {} : { near }).then(
      (response) => {
        running.current = false
        setState({ kind: 'results', results: response.results })
      },
      (reason: unknown) => {
        running.current = false
        // 501 where a deployment has no Places key. A promise, not a fault.
        setState(isNotImplemented(reason) ? { kind: 'unavailable' } : { kind: 'failed', error: reason })
      },
    )
  }, [client, near, query])

  const choose = useCallback(
    (place: GeocodeResult) => {
      onChoose(place)
      // The list has done its job. Leaving it up invites a second click that adds a second point.
      setState({ kind: 'idle' })
      setQuery('')
    },
    [onChoose],
  )

  return (
    <div className="place-search">
      <form
        className="place-search__form"
        onSubmit={(submitted) => {
          submitted.preventDefault()
          search()
        }}
      >
        <label className="place-search__field">
          <span className="place-search__label">Add a place by name</span>
          <input
            type="search"
            value={query}
            placeholder="Leavenworth, Stevens Pass…"
            onChange={(changed) => setQuery(changed.target.value)}
          />
        </label>
        <button type="submit" disabled={state.kind === 'searching'}>
          Search
        </button>
      </form>

      {state.kind === 'searching' && (
        <p className="place-search__note" role="status">
          Looking&hellip;
        </p>
      )}

      {state.kind === 'unavailable' && (
        <p className="place-search__note">Place search is not built yet.</p>
      )}

      {state.kind === 'failed' && (
        <p className="place-search__error" role="alert">
          {routeErrorMessage(state.error)}
        </p>
      )}

      {state.kind === 'results' && state.results.length === 0 && (
        // An ordinary answer to a typo. Calling it an error blames the rider for a spelling
        // mistake, and a 200 with nothing in it is correct on both sides.
        <p className="place-search__note">No places found.</p>
      )}

      {state.kind === 'results' && state.results.length > 0 && (
        <ul className="place-search__results">
          {state.results.map((place) => (
            <li key={place.place_id}>
              <button type="button" className="place-search__result" onClick={() => choose(place)}>
                {/* The name Places uses rather than what was typed, or somebody searching
                    "woodinville" wonders whether they got what they asked for. */}
                <span className="place-search__name">{place.name}</span>
                <span className="place-search__kind">{kindOf(place.kinds)}</span>
                {place.address !== null && place.address !== undefined && (
                  // What makes two places of the same name choosable at all.
                  <span className="place-search__address">{place.address}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
