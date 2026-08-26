/**
 * What a rider sees during a run that takes minutes.
 *
 * The complaint this answers was "I have no idea what it's doing and it's already been like a
 * few minutes". So it shows four things: what it is doing now, prominently; what it has done,
 * receding; how far along; and how long it has been.
 *
 * Two constraints shape it. Events arrive out of order once discovery is parallelised, so the
 * meter shows the highest figure seen rather than the latest — a bar that retreats reads as
 * broken. And events will sometimes be sparse, so the animation carries the "still alive"
 * signal on its own: an indeterminate meter says working where a 0% bar says stuck.
 *
 * Deliberately not a feed. A raw append would scroll away from the rider within seconds once
 * the backend emits per category, so the current step is a heading and the earlier ones are a
 * short receding list.
 */
import type { ReplanStep } from './useReplan'

export interface ReplanProgressProps {
  readonly isRunning: boolean
  /** Newest first. */
  readonly log: readonly ReplanStep[]
  /** Highest fraction seen, or null when nothing has reported one. */
  readonly progress: number | null
  readonly elapsedS: number
}

/** Minutes and seconds: "95s" makes a rider do arithmetic about their own wait. */
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${String(seconds)}s`
  return `${String(Math.floor(seconds / 60))}m ${String(seconds % 60)}s`
}

export function ReplanProgress({
  isRunning,
  log,
  progress,
  elapsedS,
}: ReplanProgressProps): React.JSX.Element | null {
  const [current, ...earlier] = log
  if (current === undefined) return null

  const percent = progress === null ? null : Math.round(progress * 100)

  return (
    <div className="progress">
      <p
        className="progress__current"
        // A live region only while something is happening: once it has finished there is
        // nothing left to announce, and the last line stays on screen as a record.
        {...(isRunning ? { role: 'status' as const } : {})}
      >
        {current.message}
      </p>

      <div className="progress__meter">
        <div
          className={`progress__bar${percent === null ? ' progress__bar--indeterminate' : ''}`}
          role="progressbar"
          aria-label="Finding places"
          {...(percent === null
            ? {}
            : { 'aria-valuenow': percent, 'aria-valuemin': 0, 'aria-valuemax': 100 })}
          {...(percent === null ? {} : { style: { width: `${String(percent)}%` } })}
        />
      </div>

      <p className="progress__meta">
        {percent !== null && `${String(percent)}% · `}
        {formatElapsed(elapsedS)}
      </p>

      {earlier.length > 0 && (
        <ul className="progress__earlier">
          {earlier.map((step) => (
            <li key={step.id}>{step.message}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
