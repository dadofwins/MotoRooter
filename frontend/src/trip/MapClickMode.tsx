/**
 * What a click on the map does, and what mode the points it places will route with.
 *
 * Tim: *"It's fine when you want to create a route manually but then when you're clicking on POIs
 * it's annoying if it keeps adding points to the route."* Clustering made that worse in a good
 * way — now that pins are findable and reachable, a rider spends far more time clicking the map
 * without wanting a waypoint out of it.
 *
 * **A mode rather than a checkbox.** A checkbox names what a click does; a mode names what the
 * rider is doing, which is building a route or reading the map. The word is a small risk here,
 * since this app already calls routing intent a mode and the two controls sit adjacent — it
 * survives because they read as one sentence, *add points, offroad*, rather than as two settings
 * that happen to share a noun.
 *
 * The routing selector writes `Trip.default_intent`, which until now the app **read and nothing
 * set**. It seeds new segments only: every leg keeps its own intent, which is why the label says
 * "new" rather than naming the trip.
 */
import type { LegIntent } from '../api/types'
import { LEG_MODES } from './legModes'

export interface MapClickModeProps {
  /** Whether a click on the map places a waypoint. */
  readonly placing: boolean
  readonly onPlacingChange: (placing: boolean) => void
  /** The mode new segments start on — `Trip.default_intent`. */
  readonly intent: LegIntent
  readonly onIntentChange: (intent: LegIntent) => void
}

export function MapClickMode({
  placing,
  onPlacingChange,
  intent,
  onIntentChange,
}: MapClickModeProps): React.JSX.Element {
  return (
    <div className="click-mode">
      <fieldset className="click-mode__states">
        {/* Named for the rider's activity rather than for the mechanism. "Click to place points"
            describes what the mouse does; this describes what they are up to. */}
        <legend className="click-mode__legend">Map clicks</legend>
        {[
          { value: false, label: 'Browse' },
          { value: true, label: 'Add points' },
        ].map((option) => (
          <label
            key={option.label}
            className={`click-mode__state${placing === option.value ? ' click-mode__state--on' : ''}`}
          >
            <input
              type="radio"
              name="map-clicks"
              checked={placing === option.value}
              onChange={() => {
                onPlacingChange(option.value)
              }}
            />
            {option.label}
          </label>
        ))}
      </fieldset>

      <label className="click-mode__intent">
        <span className="click-mode__intent-label">New points</span>
        <select
          value={intent}
          // Nothing to apply to while browsing. Disabled rather than hidden, so the rider can see
          // what placing would do before switching to it.
          disabled={!placing}
          onChange={(changed) => {
            onIntentChange(changed.target.value as LegIntent)
          }}
        >
          {LEG_MODES.map((mode) => (
            <option key={mode.intent} value={mode.intent}>
              {mode.label}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}
