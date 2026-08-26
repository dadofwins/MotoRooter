/**
 * The front door.
 *
 * This reverses the earlier "no dialog, just place a point" decision, and both can be true:
 * that was right for an existing trip and wrong as an entrance. A rider arriving cold needs
 * somewhere to start, and click-to-create still works once they are on the map.
 *
 * Naming stays an offer rather than a toll — starting without one works, because the original
 * reasoning holds: nobody should fill in a form before putting two points on a map.
 *
 * The list is a per-browser record of slugs visited, not an account. Saying so matters: a
 * rider who clears their browser has lost a list, not their trips, and every link still works.
 * That is the difference between a shrug and a panic.
 */
import { useState } from 'react'
import type { VisitedTrip } from '../trip/useVisitedTrips'

export interface LandingProps {
  readonly trips: readonly VisitedTrip[]
  /** An empty name is legitimate: the trip gets a default and can be renamed later. */
  readonly onCreate: (name: string) => void
  readonly onOpen: (slug: string) => void
  /** Removes it from this browser's list. The trip and its link are untouched. */
  readonly onForget: (slug: string) => void
}

export function Landing({ trips, onCreate, onOpen, onForget }: LandingProps): React.JSX.Element {
  const [name, setName] = useState('')

  return (
    <div className="landing">
      <div className="landing__card">
        <h1 className="landing__title">MotoRooter</h1>
        <p className="landing__blurb">
          Plan an adventure motorcycle trip: twisties, dirt, and somewhere to sleep.
        </p>

        <form
          className="landing__start"
          onSubmit={(event) => {
            event.preventDefault()
            onCreate(name.trim())
          }}
        >
          <label className="landing__field">
            Trip name
            <input
              type="text"
              value={name}
              placeholder="WABDR North"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <button type="submit">Start a new trip</button>
        </form>

        {trips.length > 0 && (
          <section className="landing__recent" aria-label="Trips on this browser">
            <h2>Recent trips</h2>
            <ul>
              {trips.map((trip) => (
                <li key={trip.slug}>
                  <button type="button" className="landing__open" onClick={() => onOpen(trip.slug)}>
                    {trip.name}
                  </button>
                  <button
                    type="button"
                    className="landing__forget"
                    // Named explicitly, because "remove" next to a trip name reads as delete
                    // and this is not that.
                    aria-label={`Remove ${trip.name} from this list`}
                    onClick={() => onForget(trip.slug)}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
            <p className="landing__note">
              This list is kept in this browser only. Removing a trip here does not delete it,
              and its link keeps working.
            </p>
          </section>
        )}
      </div>
    </div>
  )
}
