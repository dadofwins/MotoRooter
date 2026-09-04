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

/**
 * The kinds of line the transcript can hold.
 *
 * Here rather than in the component for the same two reasons as `HANDLED_KINDS`: a runtime
 * constant exported from a component file breaks fast refresh, and a sweep that has to
 * enumerate every kind needs one list to enumerate. `Entry['kind']` is derived from this, so
 * a kind added to the rail without being added here does not compile — which is what stops a
 * styling rule from being verified against five of six kinds and assumed for the sixth.
 */
export const ENTRY_KINDS = ['you', 'assistant', 'tool', 'tool-failed', 'unavailable', 'error'] as const

export type EntryKind = (typeof ENTRY_KINDS)[number]

/**
 * Text as the transcript should draw it.
 *
 * Entries preserve whitespace now, which turns whitespace at the edges of a string from
 * something invisible into a blank line the rider can see. A model routinely ends a block with
 * a newline and nobody meant that to be a gap under the answer, so the edges are tidied while
 * the interior is left exactly alone.
 *
 * `.trim()` is the obvious call and it is the wrong one. Backend lists indent every row by two
 * spaces, and trimming takes the indent off the *first* row only — so a list that arrived
 * aligned would render with its first line out of step with the rest of it. Blank lines go;
 * the first content line's own indentation stays.
 */
export function entryText(text: string): string {
  return text.replace(/^(?:[^\S\n]*\n)+/, '').replace(/\s+$/, '')
}
