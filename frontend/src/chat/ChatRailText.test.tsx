import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
// The real stylesheet, loaded the way the app loads it. `vite.config.ts` processes this one
// file for tests so the import injects it into the document instead of being stubbed away.
import '../index.css'
import { ChatRail } from './ChatRail'
import { ENTRY_KINDS, entryText } from './chatEvents'
import { ApiNetworkError, ApiNotImplementedError } from '../api/errors'
import type { RequestOptions } from '../api/client'
import type { ChatEvent, ChatRequest } from '../api/types'

/**
 * How the transcript treats the text it is given.
 *
 * The rail used to render every entry as a bare paragraph under the default `white-space`,
 * which collapses a newline to a space. A numbered list of waypoints therefore arrived as one
 * run-on line, and so did a line break the rider had typed themselves with shift+enter — the
 * composer offers that deliberately and the transcript threw it away.
 *
 * **Why these assertions read the stylesheet.** Whether text breaks at a newline is decided by
 * CSS, and jsdom does no layout, so there is nothing to measure. Asserting on the class name
 * instead is the trap the assignment named: a test looking for `pre-wrap` in a `className`
 * passes while the rule does nothing at all. So the real `index.css` is loaded into the
 * document and the question asked of `getComputedStyle`, which is the effective rule resolved
 * through the selector the component actually renders. That fails if the declaration is
 * deleted, renamed, misspelled, or overridden by a later rule — everything short of the pixels
 * themselves, which are checked by eye in a browser instead.
 *
 * The value is never pinned to one keyword. What the rail owes a rider is that newlines survive
 * *and* a long place name still wraps, and more than one keyword delivers that; a test that
 * demanded `pre-wrap` by name would be asserting the implementation it was handed.
 */

/** `white-space` values that keep a newline as a break and still wrap a long line. */
const KEEPS_BREAKS_AND_WRAPS = new Set(['pre-wrap', 'pre-line', 'break-spaces'])

/**
 * Of those, the ones that also keep a run of spaces.
 *
 * Not decoration. The one multi-line string that reaches the transcript from the backend today
 * is the place-disambiguation list, which aligns its columns with literal spaces
 * (`chat/tools.py`, the `ToolCallFailed` raised when a name matches several places). `pre-line`
 * would keep its line breaks and collapse its alignment into ragged prose.
 */
const KEEPS_SPACES = new Set(['pre-wrap', 'break-spaces'])

function event(overrides: Partial<ChatEvent> = {}): ChatEvent {
  return { kind: 'message', message: '', tool: null, trip_changed: false, truncated: false, ...overrides }
}

function fakeClient(events: readonly ChatEvent[]) {
  const chat = vi.fn(
    // eslint-disable-next-line @typescript-eslint/require-await
    async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
      for (const item of events) yield item
    },
  )
  return { chat }
}

/** A turn that never reaches the server. Same shape the rail's own tests use for this. */
function failingClient(error: Error) {
  const chat = vi.fn(
    // eslint-disable-next-line @typescript-eslint/require-await, require-yield
    async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
      throw error
    },
  )
  return { chat }
}

function mount(client: { chat: ReturnType<typeof vi.fn> }): void {
  render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)
}

async function send(text: string): Promise<void> {
  fireEvent.change(screen.getByRole('textbox', { name: /ask the assistant/i }), {
    target: { value: text },
  })
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await Promise.resolve()
  })
}

function entries(): readonly HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>('.chat__entry')]
}

/** The one entry whose text contains `needle`. Fails loudly rather than styling the wrong node. */
function entryContaining(needle: string): HTMLElement {
  const found = entries().filter((element) => (element.textContent ?? '').includes(needle))
  expect(found).toHaveLength(1)
  return found[0] as HTMLElement
}

const WAYPOINT_LIST = [
  'Waypoints are now:',
  '  [0] Ellensburg (47.0032, -120.5478)',
  '  [1] Blewett Pass (47.3364, -120.5786)',
  '  [2] Cashmere (47.5220, -120.4685)',
].join('\n')

/** The shape `chat/tools.py` raises when a place name matches several places. */
const DISAMBIGUATION = [
  "'lone fir' matches 2 places and nothing was added. Call again with the place_id:",
  '  ChIJN1t_tDeuEmsRUsoyG83frY4  Lone Fir Campground — Okanogan-Wenatchee, WA',
  '  ChIJP3Sa8ziYEmsRUKgyFmh9AQM  Lone Fir Cemetery — 649 SE 26th Ave, Portland, OR',
].join('\n')

describe('the stylesheet the assertions are made against', () => {
  it('is really in the document, so a missing rule fails for the reason it looks like', () => {
    // Without this the file is vacuous in one specific way: if the import were stubbed back to
    // an empty string, every computed value reads '' and every assertion below fails for a
    // reason that has nothing to do with the rail. This separates the two.
    const rules = [...document.styleSheets].flatMap((sheet) => [...sheet.cssRules])
    expect(rules.length).toBeGreaterThan(50)
    expect(rules.some((rule) => rule.cssText.startsWith('.chat__entry {'))).toBe(true)
  })
})

describe('a multi-line answer in the transcript', () => {
  it('keeps its line breaks instead of collapsing into one run-on line', async () => {
    mount(fakeClient([event({ kind: 'message', message: WAYPOINT_LIST }), event({ kind: 'done' })]))

    await send('what is the route now?')

    const entry = entryContaining('Blewett Pass')
    // The text arrives intact — nothing in the component eats the newline on the way in...
    expect(entry.textContent).toContain('Ellensburg (47.0032, -120.5478)\n  [1] Blewett Pass')
    // ...and the rule that decides whether it is drawn as a break says break.
    expect(KEEPS_BREAKS_AND_WRAPS).toContain(getComputedStyle(entry).whiteSpace)
  })

  it('keeps the alignment the backend spaced its columns with', async () => {
    // Two spaces between the place_id and the name, two more indenting each row. Line breaks
    // with the columns collapsed is a list that no longer lines up, which is most of what made
    // it a list. This is what rules out `pre-line`.
    mount(
      fakeClient([
        event({ kind: 'tool_failed', tool: 'add_place', message: DISAMBIGUATION }),
        event({ kind: 'done' }),
      ]),
    )

    await send('add lone fir')

    const entry = entryContaining('Lone Fir Cemetery')
    expect(entry.textContent).toContain('ChIJN1t_tDeuEmsRUsoyG83frY4  Lone Fir Campground')
    expect(KEEPS_SPACES).toContain(getComputedStyle(entry).whiteSpace)
  })

  it('still wraps a long place name rather than widening the rail past the map', async () => {
    // The rail is a fixed 380px column beside the map (--chat-width). `pre-wrap` and a single
    // unbroken token is exactly the pair that pushes a column wider than its track, so the
    // existing `overflow-wrap: anywhere` has to survive the change rather than be replaced by
    // it. jsdom does no layout, so this asserts the two rules coexist on the element. The
    // width itself was measured in headless Chrome with a 300-character unbroken token and
    // both rules live: rail 380px (exactly --chat-width), entry scrollWidth 332 = its client
    // width, `.app` scrollWidth 1265 = clientWidth. Nothing overflows.
    const name = 'Kloochman'.repeat(12)
    mount(fakeClient([event({ kind: 'message', message: `Found ${name}` }), event({ kind: 'done' })]))

    await send('find somewhere')

    const style = getComputedStyle(entryContaining(name))
    expect(style.overflowWrap).toBe('anywhere')
    expect(KEEPS_BREAKS_AND_WRAPS).toContain(style.whiteSpace)
  })
})

describe('the rider’s own line breaks', () => {
  it('survive into the transcript, because the composer offers them on purpose', async () => {
    // shift+enter inserts a newline in the composer by deliberate design — "this is a message
    // box, not a document". Collapsing it on the way back out makes that offer a lie.
    mount(fakeClient([event({ kind: 'done' })]))

    await send('day one: Ellensburg to Cashmere\nday two: the Teanaway')

    const entry = entryContaining('day two')
    expect(entry.textContent).toBe('day one: Ellensburg to Cashmere\nday two: the Teanaway')
    expect(KEEPS_BREAKS_AND_WRAPS).toContain(getComputedStyle(entry).whiteSpace)
  })
})

describe('every kind of line the transcript can hold', () => {
  it('breaks at a newline, so no kind is verified by assumption', async () => {
    // Checked while the entry is still in the document, and only the name kept. Collecting the
    // elements and asserting at the end would be asking `getComputedStyle` about nodes that
    // `cleanup` had already detached, which is not the question.
    const seen = new Set<string>()
    const collect = (): void => {
      for (const entry of entries()) {
        const kind = [...entry.classList]
          .find((name) => name.startsWith('chat__entry--'))
          ?.slice('chat__entry--'.length)
        if (kind === undefined) continue
        seen.add(kind)
        expect(
          KEEPS_BREAKS_AND_WRAPS.has(getComputedStyle(entry).whiteSpace),
          `chat__entry--${kind} does not keep its line breaks`,
        ).toBe(true)
      }
    }

    mount(
      fakeClient([
        event({ kind: 'message', message: 'first\nsecond' }),
        event({ kind: 'tool_finished', tool: 'find_camps', message: 'Found 3 camps' }),
        event({ kind: 'tool_failed', tool: 'resolve_place', message: 'matches 2 places:\n  a\n  b' }),
        event({ kind: 'done' }),
      ]),
    )
    await send('find camps')
    collect()
    // Unmounted between scenarios: three rails in one container is three composers, and the
    // send helper would not know which one it was typing into.
    cleanup()

    render(
      <ChatRail
        client={failingClient(new ApiNotImplementedError({ detail: 'not built' }))}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
    await send('find camps')
    collect()
    cleanup()

    render(
      <ChatRail
        client={failingClient(new ApiNetworkError({ detail: 'offline' }))}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
    await send('find camps')
    collect()

    // Every kind actually appeared, so none of the assertions above was skipped in silence.
    expect([...seen].sort()).toEqual([...ENTRY_KINDS].sort())
  })
})

describe('text on its way into an entry', () => {
  it('leaves a single line exactly as it was, so a tool note gains no height', () => {
    // Tool notes are one line today. Preserving whitespace means a stray newline at either end
    // is now a blank line the rider can see, and the only entry kind that has no background to
    // sit in is the one most likely to show it as a gap.
    expect(entryText('Found 3 camps')).toBe('Found 3 camps')
  })

  it('drops a blank line the model left at the end rather than drawing it', () => {
    expect(entryText('Here is the route:\n\n  [0] Ellensburg\n\n')).toBe(
      'Here is the route:\n\n  [0] Ellensburg',
    )
  })

  it('drops blank lines at the start without stealing the first line’s indent', () => {
    // `.trim()` is the obvious call and it is wrong here: it would take the two spaces off
    // `  [0]` and leave them on `  [1]`, so a list that arrived aligned would render with its
    // first row out of step with every other row.
    expect(entryText('\n\n  [0] Ellensburg\n  [1] Cashmere')).toBe('  [0] Ellensburg\n  [1] Cashmere')
  })

  it('keeps the blank lines that are doing work in the middle', () => {
    expect(entryText('one\n\ntwo')).toBe('one\n\ntwo')
  })

  it('has nothing to say about text with no whitespace at its edges', () => {
    expect(entryText('')).toBe('')
    expect(entryText('  [0] Ellensburg')).toBe('  [0] Ellensburg')
  })
})
