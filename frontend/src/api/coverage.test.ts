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
 * Kept as a flat record so the reason travels with the name; a bare array would rot into a list
 * nobody can audit.
 */
const UNREAD: Record<string, string> = {
  // Provider capability detail the UI has no use for. The picker resolves per *intent*, so it
  // reads the intent table rather than the provider list behind it.
  alternatives: 'no UI offers alternative routes',
  daily_quota: 'quota is an ops concern; the rider is never shown one',
  per_minute_quota: 'as above',
  elevation: 'no elevation is displayed — see the standing note on ascent being unverified',
  map_matching: 'nothing uploads a recorded track yet',
  max_waypoints: 'no path can exceed it: legs span two waypoints plus any vias',
  prefers_unpaved: 'the mode picker states the intent, not the engine behind it',

  // Trip metadata with no place in the UI.
  created_at: 'the rail shows the trip name, not its age',
  schema_version: 'migrations are the backend concern; the client compiles against one shape',

  // Aggregates the frontend recomputes from legs, because it must work on unsaved edits and
  // mid-drag previews where the stored document is stale by design. Worth knowing they exist:
  // if these ever disagree with what the rail shows, this is the pair to look at.
  total_distance_m: 'recomputed from legs so it is live during an edit',
  total_paved_fraction: 'as above, via surfaceSummary',
  total_unpaved_fraction: 'as above',
  total_unknown_fraction: 'as above',

  // Suppressed on purpose. `CLAUDE.md` forbids showing climb until someone verifies it, and
  // scripts/elevation_check.py narrowed why: ORS reports 1.8-2.5x a densely-sampled true profile,
  // and neither smoothing nor sampling density explains it. Read this field only after that is
  // resolved.
  ascent_m: 'climb is not shown until the ORS ascent discrepancy is explained',

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
      .filter((field) => !new RegExp(`\\b${field}\\b`).test(source))
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
      .filter((field) => !fields.has(field) || new RegExp(`\\b${field}\\b`).test(source))
      .sort()

    expect(stale).toEqual([])
  })
})
