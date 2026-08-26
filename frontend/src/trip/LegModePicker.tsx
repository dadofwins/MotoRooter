/**
 * How one segment routes.
 *
 * The second gap the mouse-equivalence audit found: `set_leg_intent` is one of the assistant's
 * tools and there was no per-leg control at all, so chat would have been the only way to change a
 * routing mode. Mode has been per-leg in the data since the first backend branch — `TripLeg.intent`
 * was simply inert while a trip was one leg spanning everything.
 *
 * **Which modes report surface is read from the API, never hardcoded.** `CLAUDE.md` is explicit,
 * and it is explicit because a hand-kept list went stale the day the policy table repointed an
 * intent and produced an entirely grey route. So the picker asks, and it tells the rider *in the
 * options* — a warning that appears after the choice is a warning about a decision already made.
 */
import { LEG_MODES, labelFor } from './legModes'
import type { LegIntent } from '../api/types'

export interface LegModePickerProps {
  readonly legIndex: number
  readonly intent: LegIntent
  /** Where this segment starts and ends, in the rider's words. */
  readonly from: string
  readonly to: string
  /** From `GET /api/routing/capabilities`. `null` means the table has not said. */
  readonly reportsSurface: (intent: LegIntent) => boolean | null
  /**
   * Whether this mode's engine reports a riding time worth using.
   *
   * Also from the capability table, and deliberately *not* treated the same way as surface. See
   * the note in the body: one is a cost, the other is provenance.
   */
  readonly reportsTrustworthyDuration: (intent: LegIntent) => boolean | null
  readonly onChange: (legIndex: number, intent: LegIntent) => void
}

export function LegModePicker({
  legIndex,
  intent,
  from,
  to,
  reportsSurface,
  reportsTrustworthyDuration,
  onChange,
}: LegModePickerProps): React.JSX.Element {
  /** A mode with no label yet is still a mode the leg can be on, so it is offered as itself. */
  const offered: readonly LegIntent[] = LEG_MODES.some((mode) => mode.intent === intent)
    ? LEG_MODES.map((mode) => mode.intent)
    : [...LEG_MODES.map((mode) => mode.intent), intent]

  // False, not null: only an answer the table actually gave is worth putting in front of a
  // rider. Warning on "we have not loaded yet" would cry wolf on every first render.
  const losesSurface = reportsSurface(intent) === false

  /**
   * Whether the time shown for this leg is our model rather than the engine's figure.
   *
   * Stated, not warned about, and kept out of the option labels — unlike surface. Losing the
   * dirt/paved breakdown is a *cost* a rider pays for choosing Fast, so it belongs in the choice.
   * Which clock produced the estimate is *provenance*, and on dirt ours is the better number:
   * hosted ORS reported 143 min for a 40 km leg that takes about 46. Marking every offroad option
   * as a warning would teach a rider the opposite of the truth.
   */
  const timeIsModelled = reportsTrustworthyDuration(intent) === false

  return (
    <div className="leg-mode">
      <label className="leg-mode__field">
        <span className="leg-mode__label">{`${from} to ${to}`}</span>
        <select
          value={intent}
          onChange={(changed) => onChange(legIndex, changed.target.value as LegIntent)}
        >
          {offered.map((option) => {
            const blind = reportsSurface(option) === false
            return (
              <option key={option} value={option}>
                {/* In the option itself, because this is the moment of choosing. */}
                {blind ? `${labelFor(option)} — no surface data` : labelFor(option)}
              </option>
            )
          })}
        </select>
      </label>

      {losesSurface && (
        <p className="leg-mode__note">
          This engine reports no dirt or paved breakdown, so this segment will draw as unsurveyed.
        </p>
      )}

      {timeIsModelled && (
        <p className="leg-mode__provenance">
          Riding time here is our own estimate from distance and surface — this engine reports
          bicycle times.
        </p>
      )}
    </div>
  )
}
