/**
 * The chat event kinds the rail handles, named so a test can check nothing is missing.
 *
 * Deliberately not an exhaustive `switch` with a `never` default. That would make an *additive*
 * contract change red on this side before anyone could act on it — the coupling the `Omit` bridge
 * exists to avoid on a field removal, and there is no equivalent trick for a union member. A test
 * gives the same information, names the kind and points at the rail, and fails in CI rather than
 * in the producer's build, which is where an unhandled kind is this side's problem to fix.
 *
 * In its own module because a component file that also exports constants breaks fast refresh —
 * and because a tripwire that lives beside the thing it guards is easier to ignore than one that
 * has its own name.
 */
export const HANDLED_KINDS = [
  'message',
  'tool_started',
  'tool_progress',
  'tool_finished',
  'tool_failed',
  'done',
] as const

/**
 * A readable name for a tool that did not describe itself.
 *
 * `tool_started` carries an empty message on the wire — measured against the live stack, not
 * inferred — so without this a rider watching `describe_trip` run is told only "Working…".
 *
 * Derived from the identifier the backend sent rather than from a table of phrases written here.
 * A hand-written label per tool would duplicate wording the backend owns and drift from it the
 * first time either side changed one, which is why the rail refused to do that when it was built.
 * This cannot drift: it is the wire value with its underscores taken out, and it is superseded
 * the moment a tool sends a real note.
 */
export function toolActivityLabel(tool: string | null | undefined): string | null {
  const name = (tool ?? '').trim()
  if (name === '') return null
  return name.replaceAll('_', ' ')
}
