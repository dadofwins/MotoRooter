/**
 * The routing modes as a rider sees them.
 *
 * Three names, chosen by Tim, mapping onto the existing `LegIntent` rather than a parallel
 * vocabulary. The labels are a frontend concern — what to *call* a mode is product language —
 * but nothing here says which engine serves one or what it can do. That is the backend's policy
 * table, read at runtime from `GET /api/routing/capabilities`.
 *
 * `technical_offroad` and `manual_track` stay unlabelled for now. The field expresses all five,
 * so adding a label later is a label, not a migration — and offering a mode nobody has decided
 * the meaning of is worse than not offering it.
 */
import type { LegIntent } from '../api/types'

export interface LegMode {
  readonly intent: LegIntent
  readonly label: string
  /** What choosing it is for, in the rider's terms rather than the engine's. */
  readonly hint: string
}

export const LEG_MODES: readonly LegMode[] = [
  { intent: 'highway_connector', label: 'Fast', hint: 'Get there. Main roads, fewest turns.' },
  { intent: 'twisty_paved', label: 'Twisties', hint: 'Paved and bendy. The fun tarmac.' },
  { intent: 'unpaved', label: 'Offroad', hint: 'Prefers dirt and gravel where there is any.' },
]

/**
 * The rider-facing name for an intent, or the raw value when it has none.
 *
 * Falling back to the intent rather than to "Unknown": a leg on `technical_offroad` is on a real
 * mode that simply has no label yet, and showing a blank would read as a bug.
 */
export function labelFor(intent: LegIntent): string {
  return LEG_MODES.find((mode) => mode.intent === intent)?.label ?? intent
}
