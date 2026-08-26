import { describe, expect, it } from 'vitest'
import spec from '../../../shared/openapi.json'

/**
 * Every contract field is either read by the app or listed here as deliberately unread.
 *
 * Eight times in one day, a backend field landed and nothing consumed it. Every instance had the
 * same shape — a producer merged, a consumer assumed — and three of them were rider-facing:
 * `reports_surface` would have let the mode picker warn and did not, `duration_is_trustworthy`
 * was stamped on every leg while the estimate ignored it, and `duration_is_estimated` existed so
 * a rider could tell a measurement from a model and said nothing.
 *
 * A mechanical check cannot know whether a field *should* be read. But it can insist that the
 * answer is written down, which turns "nobody noticed" into "somebody decided". That is the same
 * trick as the backend's `OPTIONAL_SERVICES` tuple: a list is a poor abstraction and a good
 * tripwire.
 *
 * **When this fails, the fix is usually to consume the field, not to add it below.** Adding a
 * name is a claim that the frontend has no business with it — write the reason next to it.
 */

/**
 * Fields the frontend deliberately does not read, and why.
 *
 * This list caught its own author within an hour of being merged. `ascent_m` was written off here
 * as "not shown until the ORS ascent discrepancy is explained" — and the discrepancy was explained
 * the same afternoon, which made the entry a decision whose reason had expired while still reading
 * as considered. An expired reason is worse than no entry, and a bare list of names could not have
 * shown that.
 *
 * Kept as a flat record so the reason travels with the name; a bare array would rot into a list
 * nobody can audit.
 */
const UNREAD: Record<string, string> = {
  // Provider capability detail the UI has no use for. The picker resolves per *intent*, so it
  // reads the intent table rather than the provider list behind it.
  alternatives: 'no UI offers alternative routes',
  daily_quota: 'quota is an ops concern; the rider is never shown one',
  per_minute_quota: 'as above',
  map_matching: 'nothing uploads a recorded track yet',
  max_waypoints: 'no path can exceed it: legs span two waypoints plus any vias',
  prefers_unpaved: 'the mode picker states the intent, not the engine behind it',

  // Trip metadata with no place in the UI.
  // STAGING — integrator, on the be/offroad-means-offroad merge. `fe/default-intent` consumes it:
  // legStructure.ts holds its own DEFAULT_INTENT, which was the *right value* and the wrong
  // place — the second answer to a question the trip document now answers once. Expires by
  // itself when that branch reads the field.
  default_intent: 'consumed by fe/default-intent, replacing the local DEFAULT_INTENT constant',

  // STAGING — integrator, on the be/route-through-finds merge. `fe/route-through-best` consumes
  // all three: `score` and the judge's reason are what let the button say *why* a place was
  // added, `left_out` is what lets it say why the others were not, and `limit` is the override
  // for a rider who wants more than the default. Expire when that branch lands.
  score: 'consumed by fe/route-through-best — shown as why a place was chosen',
  left_out: 'consumed by fe/route-through-best — shown as why the rest were not',
  limit: 'written by fe/route-through-best — the override for "add more than the default"',

  created_at: 'the rail shows the trip name, not its age',
  schema_version: 'migrations are the backend concern; the client compiles against one shape',

  // Aggregates the frontend recomputes from legs, because it must work on unsaved edits and
  // mid-drag previews where the stored document is stale by design. Worth knowing they exist:
  // if these ever disagree with what the rail shows, this is the pair to look at.
  total_distance_m: 'recomputed from legs so it is live during an edit',
  total_paved_fraction: 'as above, via surfaceSummary',
  total_unpaved_fraction: 'as above',
  total_unknown_fraction: 'as above',

  // The engine's own figure, which the frontend deliberately never chooses between. Whether it
  // can be believed is a capability, and the backend resolves it in one place — reading it here
  // would put the same judgement in two implementations.
  duration_s: 'the backend decides engine-versus-model; the client shows the answer',

  // Derived by `needsReplan` instead, because it is serialised on TripSummary and not on Trip —
  // so a trip being edited has no field to read. Mirrored deliberately, and commented there.
  needs_replan: 'derived from edited_at and planned_at, which Trip does carry',

  // Requested but never sent: the chat rail cannot yet steer a replan with free text. This one is
  // a real gap rather than a decision, and it is written down here so it stays visible.
  prompt: 'no UI sends a free-text steer to discovery yet',
}

/**
 * Source with comments removed.
 *
 * Found by mutating the check itself: deleting the one real read of `duration_is_estimated` left
 * it passing, because a *docstring* elsewhere mentioned the field by name. A guard satisfied by
 * prose about a field is worse than no guard — it reports coverage that does not exist.
 */
function withoutComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/\/\/[^\n]*/g, ' ')
}

/**
 * Whether the source actually *uses* a field, rather than merely containing the word.
 *
 * A property access, a string key, or a destructured binding — not any occurrence of the name.
 * A bare word match reports a field as consumed when its name collides with a module or a local
 * variable, and that is not hypothetical: auditing the backend for the same class of bug,
 * `ReplanRequest.prompt` looked consumed because `motorooter.chat.prompt` is a module path. The
 * field is accepted by the endpoint and silently dropped.
 *
 * Today both forms agree on every field here, so this costs nothing and closes the hole before it
 * hides something. The residual risk runs the safe way: a field read through an unusual shape
 * reports as unread, which forces a decision rather than hiding one.
 */
function isUsed(field: string, source: string): boolean {
  return (
    new RegExp(`\\.${field}\\b`).test(source) ||
    new RegExp(`['"]${field}['"]`).test(source) ||
    new RegExp(`\\b${field}\\s*[,:}]`).test(source)
  )
}

/** Field names the contract declares, across every schema. */
function contractFields(): Set<string> {
  const schemas = spec.components.schemas as Record<string, { properties?: Record<string, unknown> }>
  const names = new Set<string>()
  for (const schema of Object.values(schemas)) {
    for (const field of Object.keys(schema.properties ?? {})) names.add(field)
  }
  return names
}

/**
 * Application source, as text.
 *
 * Read through Vite's own `import.meta.glob` rather than `node:fs`, so the check needs no Node
 * type declarations in a project that deliberately has none — pulling in `@types/node` to run one
 * test would let server APIs autocomplete in browser code.
 *
 * Generated types name every field by definition, and the fixtures set every field so a test can
 * ignore the ones it does not care about — counting either would make the check vacuous. Tests are
 * excluded too: a field asserted in a test but never rendered is exactly the bug.
 */
const SOURCES = import.meta.glob('../**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
})

function appSource(): string {
  return Object.entries(SOURCES)
    .filter(
      ([path]) =>
        !/\.test\.tsx?$/.test(path) && !path.endsWith('schema.ts') && !path.endsWith('fixtures.ts'),
    )
    .map(([, text]) => text)
    .join('\n')
}

describe('contract coverage', () => {
  it('reads every field it has not written off', () => {
    const source = withoutComments(appSource())

    const unconsumed = [...contractFields()]
      .filter((field) => !isUsed(field, source))
      .filter((field) => !(field in UNREAD))
      .sort()

    expect(unconsumed).toEqual([])
  })

  it('has no stale entries in the written-off list', () => {
    // A field that started being read, or was removed from the contract, should not keep its
    // excuse — otherwise the list grows into exactly the unauditable thing it exists to prevent.
    const fields = contractFields()
    const source = withoutComments(appSource())

    const stale = Object.keys(UNREAD)
      .filter((field) => !fields.has(field) || isUsed(field, source))
      .sort()

    expect(stale).toEqual([])
  })
})
