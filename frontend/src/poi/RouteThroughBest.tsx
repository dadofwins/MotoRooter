/**
 * Put the best-ranked discovered places on the route, in one action.
 *
 * The mouse equivalent of the assistant's "route through the ones that rank highly", and the last
 * thing keeping that from being a chat-only capability. Its own control rather than an option on
 * Replan, because the judgement is already persisted on the POIs — `score`, and the judge's own
 * sentence in `note` — so this needs no search and a rider can change their mind without paying
 * sixty seconds for discovery again.
 *
 * Three things it has to say, and it has no model to say them:
 *
 * **Why these.** The judge's sentence, the same one the assistant would quote back.
 *
 * **Why not the others.** A bound the rider cannot see reads as the search having found nothing
 * else, which makes a deliberately conservative default look like a poor result.
 *
 * **How to undo it.** Every other route change in this app is one click per point. This one moves
 * several at once on the strength of a number the rider never saw, so it has to be reversible —
 * and the backend has already saved it, so undoing means writing the previous version back rather
 * than merely forgetting.
 */
import { useCallback, useRef, useState } from 'react'
import type { ApiClient } from '../api/client'
import { routeErrorMessage } from '../trip/routeErrorMessage'
import type { Poi, Trip } from '../api/types'

export type BestRouter = Pick<ApiClient, 'routeThroughBest'>

export interface RouteThroughBestProps {
  readonly client: BestRouter
  readonly slug: string
  /** How many places are available to choose from. Nothing to offer when there are none. */
  readonly candidates: number
  /** The saved trip, handed back so the map can redraw without another read. */
  readonly onRouted: (trip: Trip) => void
  /** Put the route back as it was. The change is already persisted, so this writes. */
  readonly onUndo: () => void
}

interface Outcome {
  readonly added: readonly Poi[]
  readonly leftOut: readonly Poi[]
}

export function RouteThroughBest({
  client,
  slug,
  candidates,
  onRouted,
  onUndo,
}: RouteThroughBestProps): React.JSX.Element | null {
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [error, setError] = useState<unknown>(null)
  const running = useRef(false)

  const run = useCallback(
    (limit?: number) => {
      if (running.current) return
      running.current = true
      setError(null)

      client
        .routeThroughBest(slug, limit === undefined ? {} : { limit }, {})
        .then(
          (result) => {
            running.current = false
            setOutcome({ added: result.added, leftOut: result.left_out })
            onRouted(result.trip)
          },
          (reason: unknown) => {
            running.current = false
            setError(reason)
          },
        )
    },
    [client, slug, onRouted],
  )

  // Nothing to offer, so nothing to show. A button that would add nothing reads as an action
  // that failed.
  if (candidates === 0) return null

  return (
    <div className="best-route">
      <button
        type="button"
        className="best-route__go"
        onClick={() => {
          run()
        }}
      >
        Route through the best
      </button>

      {error !== null && (
        <p className="best-route__error" role="alert">
          {routeErrorMessage(error)}
        </p>
      )}

      {outcome !== null && outcome.added.length === 0 && (
        // A real outcome on a short trip or a thin corridor. Silence would read as a broken
        // button.
        <p className="best-route__note">None of them were worth the detour.</p>
      )}

      {outcome !== null && outcome.added.length > 0 && (
        <div className="best-route__outcome">
          <p className="best-route__summary">
            {`Added ${String(outcome.added.length)} place${outcome.added.length === 1 ? '' : 's'}.`}{' '}
            <button
              type="button"
              className="best-route__undo"
              onClick={() => {
                setOutcome(null)
                onUndo()
              }}
            >
              Undo
            </button>
          </p>

          <ul className="best-route__added">
            {outcome.added.map((place) => (
              <li key={place.id}>
                <span className="best-route__name">{place.name}</span>
                {place.note !== null && place.note !== undefined && place.note !== '' && (
                  // The judge's own words, which are why this place and not another.
                  <span className="best-route__why"> — {place.note}</span>
                )}
              </li>
            ))}
          </ul>

          {outcome.leftOut.length > 0 && (
            <p className="best-route__note">
              {`${String(outcome.leftOut.length)} more were good enough but did not fit.`}{' '}
              <button
                type="button"
                className="best-route__more"
                onClick={() => {
                  // Everything already on plus everything left out, rather than a number from
                  // nowhere: the rider is asking for the ones they can see were excluded.
                  run(outcome.added.length + outcome.leftOut.length)
                }}
              >
                {`Add ${String(outcome.leftOut.length)} more`}
              </button>
            </p>
          )}
        </div>
      )}
    </div>
  )
}
