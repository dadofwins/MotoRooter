/**
 * The surface breakdown in words.
 *
 * The map draws three states; this says them as numbers, where a rider planning fuel and
 * tyres will actually read them. The rule it exists to keep is that unsurveyed distance
 * never folds into paved — on a real route that is roughly a third of the distance, and it
 * is a materially different proposition from tarmac.
 *
 * Design decisions, in the order the dataviz method takes them:
 *
 * - **Form**: a single horizontal stacked bar. The data's job is part-to-whole of one total
 *   with three segments, which is what a stacked bar is for; three slices is too few for a
 *   pie to beat a bar, and in a 380px rail a bar plus labelled rows fits where a legend
 *   around a circle would not.
 * - **Colour**: the same two hues the route line uses, so the rider learns one encoding
 *   rather than two. Validated with the skill's checker in both modes — worst adjacent pair
 *   ΔE 30.2 protan, 28.9 tritan, contrast ≥ 3:1 on both surfaces. Unknown stays neutral
 *   grey: it fails the chroma floor on purpose, because greyness *is* the meaning, and it is
 *   an absence of data rather than a third identity.
 * - **Not colour alone**: every share is named in text beside its swatch, and the list —
 *   not the bar — is the accessible representation. Texture is left to `forced-colors` and
 *   print, per the method, rather than hatching a 10px bar by default.
 * - **No tooltip**: the method's hover layer exists to carry values the plot cannot show.
 *   Here every value is already directly labelled, so there is nothing left to reveal, and
 *   hover is no use on the phone half of this app anyway.
 */
import type { Surface, TripLeg } from '../api/types'
import { formatDistance, type DistanceUnit } from '../units/format'
import { summariseSurface, toWholePercentages } from './surfaceSummary'

/** Dirt first: it is what the rider came for. */
const ORDER: readonly Surface[] = ['unpaved', 'paved', 'unknown']

const LABELS: Record<Surface, string> = {
  unpaved: 'Unpaved',
  paved: 'Paved',
  // Describes the data, not the road: nobody has surveyed it, which is not the same as it
  // being rough or being smooth.
  unknown: 'Unsurveyed',
}

export interface SurfaceSummaryProps {
  readonly legs: readonly TripLeg[]
  /** Passed in rather than read here: one source of truth for the unit, app-wide. */
  readonly unit: DistanceUnit
}

export function SurfaceSummary({ legs, unit }: SurfaceSummaryProps): React.JSX.Element | null {
  const summary = summariseSurface(legs)
  if (summary === null) return null

  const percentages = toWholePercentages(summary.fractions)
  const shown = ORDER.filter((surface) => percentages[surface] > 0)

  return (
    <section className="surface" aria-label="Surface breakdown">
      {/* Decorative: it is a picture of the list below, and a screen reader that read both
          would say everything twice. */}
      <div className="surface__bar" aria-hidden="true">
        {shown.map((surface) => (
          <span
            key={surface}
            className={`surface__segment surface__segment--${surface}`}
            style={{ flexGrow: percentages[surface] }}
          />
        ))}
      </div>

      <ul className="surface__legend">
        {shown.map((surface) => (
          <li key={surface}>
            <span className={`surface__swatch surface__swatch--${surface}`} aria-hidden="true" />
            <span className="surface__label">{LABELS[surface]}</span>
            <span className="surface__share">{percentages[surface]}%</span>
            {/* A percentage is not a plan: 40% means nothing until it is 120 km. */}
            <span className="surface__distance">{formatDistance(summary.distanceM[surface], unit)}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
