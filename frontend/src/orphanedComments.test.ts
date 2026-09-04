import { describe, expect, it } from 'vitest'

/**
 * A comment that is documenting nothing, because something was inserted beneath it.
 *
 * Adding a declaration immediately above an existing one silently reassigns the comment that
 * was documenting it: the new code takes the doc block, and the thing it was written for is
 * left bare. Nothing fails — types pass, lint passes, tests pass — and the diff reads clean,
 * because the orphaned line appears in the hunk as *unchanged context*. You only see it by
 * looking above the insertion point, which is the one place a reader of a diff is not looking.
 *
 * **This is a guard rather than a rule, and it is here because attention demonstrably does not
 * do the job.** It was found twice by review and then measured, and the measurement was worse
 * than either of us guessed: six instances in `src`, four of which nobody had ever seen. One
 * of those sits a hundred lines below an instance that had just been found by review and fixed
 * — both of us read that file closely during the exchange and neither saw the second. That is
 * the same lesson this project already learned about diffs and about contract coverage,
 * arriving somewhere new: a reader paying attention still cannot see what is not in front of
 * them.
 *
 * **The blank line is the whole discriminator, and it is not arbitrary.** A file-level doc
 * block followed by a declaration's doc block is normal and always has a blank line between
 * them — nine such pairs in `src` when this was written. An orphaned comment never does,
 * because the insertion went in tight against the thing below it. Measured 2026-09-04:
 * 15 adjacent pairs in total, 9 legitimate with a blank line, 6 without, and all 6 of those
 * were the fault. Zero false positives is why this exists; if that ratio moves, this costs
 * more than it saves and should be retired rather than tuned.
 *
 * **The other shape of the same fault does not occur here.** A line comment orphaned by a doc
 * block below it is the same mistake and would need the same check — measured 2026-09-04 and
 * there were **zero** instances in `src`, which is why this rule is the whole of the observed
 * fault rather than half of it. The zero is recorded because it is the kind of fact that stops
 * being true quietly: anyone widening this later should know what the number was when it was
 * narrowed.
 *
 * **A blank line silences this, and that is the correct weakness.** Unlike the contract
 * coverage tripwire — which cannot be satisfied without writing a reason next to a name — this
 * one has a one-keystroke escape hatch. That is the right guarantee for the problem: it
 * catches an *accident*, and an accident does not reach for an escape hatch. Someone who adds
 * a blank line has separated the blocks deliberately, and the result reads as deliberate to
 * the next person. Do not mistake this for a rule that cannot be subverted, and do not rip it
 * out on discovering that it can.
 */

/**
 * Every source file, as text.
 *
 * Vite's own glob rather than `node:fs`, because `tsconfig.json` keeps an explicit `types`
 * list so an `@types` package only lands in scope when it is wanted — and pulling in all of
 * `@types/node` to read some files would put `process` and `Buffer` in scope everywhere as a
 * side effect. `import.meta.glob` is typed by `vite/client`, which is already on that list.
 *
 * `?raw` means these are strings: nothing here is imported as a module, so globbing the whole
 * tree costs no execution and cannot create a cycle with the files it reads.
 */
const SOURCES: Record<string, string> = import.meta.glob('./**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
})

/** Generated, and not ours to format. */
const GENERATED = /\/schema\.ts$/

/**
 * A doc block closing and another opening with no blank line between them.
 *
 * Deliberately anchored on the absence of a blank line rather than on any whitespace — see the
 * measurement above. `[ \t]*` allows the indentation the second block sits at and nothing else.
 */
const ORPHANED = /\*\/\n[ \t]*\/\*\*/g

describe('comments that document nothing', () => {
  it('is really reading the tree, so a pass means checked rather than skipped', () => {
    // A glob that matched nothing passes this file in its entirety and looks exactly like a
    // clean tree. Same fault as a stylesheet that failed to load, and the same cheap answer.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50)
    expect(Object.values(SOURCES).every((text) => text.length > 0)).toBe(true)
  })

  it('has none: every doc block has a declaration under it', () => {
    const found = Object.entries(SOURCES)
      .filter(([file]) => !GENERATED.test(file))
      .flatMap(([file, text]) =>
        [...text.matchAll(ORPHANED)].map(
          // The line of the *orphaned* block's closing brace, which is where a reader has to
          // look. Pointing at the second block would name the code that is documented fine.
          (match) => `${file}:${String(text.slice(0, match.index).split('\n').length)}`,
        ),
      )

    expect(found).toEqual([])
  })
})
