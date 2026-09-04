import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ChatRail } from './ChatRail'
import { HANDLED_KINDS } from './chatEvents'
import spec from '../../../shared/openapi.json'
import { ApiNetworkError, ApiNotImplementedError } from '../api/errors'
import type { RequestOptions } from '../api/client'
import type { ChatEvent, ChatRequest } from '../api/types'

/**
 * The assistant rail.
 *
 * Chat is an accelerator, never a requirement: everything here is a second path to something
 * the mouse already does. So the rail's job is not to own trip state — it is to say what the
 * assistant is doing, and to tell the app when the trip changed underneath it.
 *
 * The distinctions that carry weight are all about a turn that has not finished cleanly. A
 * rider waiting needs to tell "working" from "finished" from "cut off", and those look
 * identical if the rail only shows text.
 */

function event(overrides: Partial<ChatEvent> = {}): ChatEvent {
  return { kind: 'message', message: '', tool: null, trip_changed: false, truncated: false, ...overrides }
}

/** A client whose turn emits exactly these events, then ends. */
function fakeClient(events: readonly ChatEvent[]) {
  const chat = vi.fn(
    // eslint-disable-next-line @typescript-eslint/require-await
    async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
      for (const item of events) yield item
    },
  )
  return { chat }
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


/** `send`, but without the real-promise wait that fake timers would hang on. */
async function sendWithTimers(text: string): Promise<void> {
  fireEvent.change(screen.getByRole('textbox', { name: /ask the assistant/i }), {
    target: { value: text },
  })
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await Promise.resolve()
  })
}

describe('ChatRail', () => {
  it('opens by saying both ways of starting a trip', () => {
    render(<ChatRail client={fakeClient([])} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    expect(screen.getByText(/describe your trip/i)).toBeInTheDocument()
    expect(screen.getByText(/set a start and end point on the map/i)).toBeInTheDocument()
  })

  it('shows what the rider asked and what the assistant answered', async () => {
    const client = fakeClient([
      event({ kind: 'message', message: 'There is a campground at Lone Fir.' }),
      event({ kind: 'done' }),
    ])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('anywhere to camp?')

    expect(await screen.findByText('anywhere to camp?')).toBeInTheDocument()
    expect(await screen.findByText(/campground at Lone Fir/)).toBeInTheDocument()
  })

  it('keeps what a tool did, not that it began', async () => {
    // Both used to land in the transcript. Starting is what is *happening* and lives on the
    // activity line, which is gone by the time the turn ends; what happened is worth keeping.
    // Two lines per tool would turn a six-tool turn into the log Tim asked to have folded away.
    const client = fakeClient([
      event({ kind: 'tool_started', message: 'Searching for camps', tool: 'find_camps' }),
      event({ kind: 'tool_finished', message: 'Found 3 camps', tool: 'find_camps' }),
      event({ kind: 'done' }),
    ])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('find camps')

    expect(await screen.findByText(/Found 3 camps/)).toBeInTheDocument()
    expect(screen.queryByText(/Searching for camps/)).not.toBeInTheDocument()
  })

  it('marks a failed tool as failed rather than folding it into the answer', async () => {
    const client = fakeClient([
      event({ kind: 'tool_failed', message: 'Places lookup failed', tool: 'resolve_place' }),
      event({ kind: 'done' }),
    ])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('find camps')

    expect(await screen.findByRole('alert')).toHaveTextContent(/Places lookup failed/)
  })

  it('tells the app to re-read the trip rather than reconstructing the change', async () => {
    // The mouse path and the chat path must converge on one document. Replaying events into
    // local state would make two models of it, and they would diverge silently.
    const onTripChanged = vi.fn()
    const client = fakeClient([
      event({ kind: 'tool_finished', message: 'Added Lone Fir', tool: 'add_poi', trip_changed: true }),
      event({ kind: 'done', trip_changed: true }),
    ])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={onTripChanged} />)

    await send('add Lone Fir')

    await waitFor(() => expect(onTripChanged).toHaveBeenCalled())
  })

  it('says when a turn was cut off, which is not the same as finished', async () => {
    // `truncated` is on the terminal event for exactly this reason: someone waiting needs to
    // know the assistant stopped mid-task rather than having nothing left to say.
    const client = fakeClient([
      event({ kind: 'message', message: 'I found two so far' }),
      event({ kind: 'done', truncated: true }),
    ])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('plan the whole week')

    expect(await screen.findByText(/cut off|stopped early|did not finish/i)).toBeInTheDocument()
  })

  it('does not claim truncation on a turn that ended normally', async () => {
    const client = fakeClient([event({ kind: 'message', message: 'Done.' }), event({ kind: 'done' })])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('anything else?')

    await waitFor(() => expect(screen.getByText('Done.')).toBeInTheDocument())
    expect(screen.queryByText(/cut off|stopped early/i)).not.toBeInTheDocument()
  })

  it('sends the transcript back, oldest first, so the assistant sees the conversation', async () => {
    const client = fakeClient([event({ kind: 'message', message: 'Sure.' }), event({ kind: 'done' })])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('first question')
    await waitFor(() => expect(screen.getByText('Sure.')).toBeInTheDocument())
    await send('second question')

    await waitFor(() => expect(client.chat).toHaveBeenCalledTimes(2))
    expect(client.chat.mock.calls[1]?.[1].history).toEqual([
      { role: 'user', content: 'first question' },
      { role: 'assistant', content: 'Sure.' },
    ])
  })

  it('will not send an empty message', async () => {
    const client = fakeClient([event({ kind: 'done' })])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /send/i }))
      await Promise.resolve()
    })

    expect(client.chat).not.toHaveBeenCalled()
  })

  it('refuses a second turn while one is still running', async () => {
    // The server is stateless and the transcript is the client's; two turns in flight would
    // race to append to it.
    const client = {
      chat: vi.fn(
        // eslint-disable-next-line require-yield
        async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
          await new Promise(() => undefined)
        },
      ),
    }
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('first')

    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
  })

  it('abandons a turn in flight when it unmounts', async () => {
    // A turn outliving its rail delivers events into a dead tree, and on the way it spends an
    // OpenAI call plus whatever tools it decides to run. Leaving the trip screen has to stop it.
    const signals: AbortSignal[] = []
    const client = {
      chat: vi.fn(
        // eslint-disable-next-line require-yield
        async function* (_slug: string, _request: ChatRequest, options?: RequestOptions) {
          if (options?.signal !== undefined) signals.push(options.signal)
          await new Promise(() => undefined)
        },
      ),
    }
    const view = render(
      <ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />,
    )
    await send('plan me a week')
    await waitFor(() => expect(signals).toHaveLength(1))

    view.unmount()

    expect(signals[0]?.aborted).toBe(true)
  })

  it('presents the 501 stub as not built yet, not as a failure', async () => {
    const client = {
      chat: vi.fn(
        // eslint-disable-next-line @typescript-eslint/require-await, require-yield
        async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
          throw new ApiNotImplementedError({ detail: 'chat is not implemented yet' })
        },
      ),
    }
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('hello')

    expect(await screen.findByText(/not built yet|not available yet/i)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('reports a real failure as a failure, without an internal string', async () => {
    const client = {
      chat: vi.fn(
        // eslint-disable-next-line @typescript-eslint/require-await, require-yield
        async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
          throw new ApiNetworkError({ detail: 'Failed to fetch' })
        },
      ),
    }
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('hello')

    expect(await screen.findByRole('alert')).toHaveTextContent(/connection|reach/i)
  })

  it('brings a trip into existence rather than refusing the first message', async () => {
    // Chat is addressed by trip slug, and the opening line invites the rider to *describe* a
    // trip before placing anything. Disabling the input until a trip is saved would make the
    // app's own first sentence unreachable, so the first message creates the document it needs.
    const resolveSlug = vi.fn(() => Promise.resolve('trip-abc123'))
    const client = fakeClient([event({ kind: 'done' })])
    render(<ChatRail client={client} resolveSlug={resolveSlug} onTripChanged={vi.fn()} />)

    expect(screen.getByRole('textbox', { name: /ask the assistant/i })).not.toBeDisabled()
    await send('a weekend of dirt near Leavenworth')

    await waitFor(() => expect(client.chat).toHaveBeenCalledTimes(1))
    expect(client.chat.mock.calls[0]?.[0]).toBe('trip-abc123')
  })

  it('says so plainly when the trip cannot be created', async () => {
    const resolveSlug = vi.fn(() => Promise.reject(new ApiNetworkError({ detail: 'Failed to fetch' })))
    const client = fakeClient([event({ kind: 'done' })])
    render(<ChatRail client={client} resolveSlug={resolveSlug} onTripChanged={vi.fn()} />)

    await send('hello')

    expect(await screen.findByRole('alert')).toHaveTextContent(/connection|reach/i)
    expect(client.chat).not.toHaveBeenCalled()
  })
})

/**
 * Following the conversation.
 *
 * Tim's item 3, from planning a real trip: the history should auto-scroll to the bottom. The care
 * it needs is the other half — a rider who has scrolled up to re-read something must not be yanked
 * back down the moment the assistant says anything, which is the standard way this feature becomes
 * infuriating.
 *
 * jsdom has no layout engine, so the scroll geometry is defined by the test. That is honest here
 * rather than a workaround: the component only ever reads three numbers off the element, and
 * supplying them is exactly what a browser would do.
 */
describe('ChatRail scrolling', () => {
  /** Gives the log a size, since jsdom reports every dimension as zero. */
  function measure(log: HTMLElement, { height = 200, content = 1000, top = 800 } = {}) {
    Object.defineProperty(log, 'clientHeight', { value: height, configurable: true })
    Object.defineProperty(log, 'scrollHeight', { value: content, configurable: true })
    log.scrollTop = top
    return log
  }

  function theLog(): HTMLElement {
    const log = document.querySelector('.chat__log')
    if (log === null) throw new Error('no chat log')
    return log as HTMLElement
  }

  it('scrolls to the newest message', async () => {
    const client = fakeClient([
      event({ kind: 'message', message: 'There is a campground at Lone Fir.' }),
      event({ kind: 'done' }),
    ])
    render(
      <ChatRail
        client={client}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
    measure(theLog())

    await send('anywhere to camp?')
    await screen.findByText(/campground at Lone Fir/)

    expect(theLog().scrollTop).toBe(1000)
  })

  it('leaves a rider alone who has scrolled up to read something', async () => {
    // The half that makes it bearable. Yanking someone back mid-sentence is worse than not
    // scrolling at all, because it happens exactly when they are concentrating.
    const client = fakeClient([
      event({ kind: 'message', message: 'There is a campground at Lone Fir.' }),
      event({ kind: 'done' }),
    ])
    render(
      <ChatRail
        client={client}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
    const log = measure(theLog(), { top: 0 })
    fireEvent.scroll(log)

    await send('anywhere to camp?')
    await screen.findByText(/campground at Lone Fir/)

    expect(theLog().scrollTop).toBe(0)
  })

  it('follows again once they scroll back down', async () => {
    // Returning to the bottom is how a rider says "carry on" — no separate control needed, and
    // no state that can get stuck.
    const client = fakeClient([
      event({ kind: 'message', message: 'First answer.' }),
      event({ kind: 'done' }),
    ])
    render(
      <ChatRail
        client={client}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
    const log = measure(theLog(), { top: 0 })
    fireEvent.scroll(log)

    log.scrollTop = 800
    fireEvent.scroll(log)
    await send('anything else?')
    await screen.findByText('First answer.')

    expect(theLog().scrollTop).toBe(1000)
  })

  it('counts a few pixels off the bottom as still following', async () => {
    // Rounding, momentum and sub-pixel heights mean "at the bottom" is never exact, and an exact
    // comparison would silently stop following after one flick of a trackpad.
    const client = fakeClient([
      event({ kind: 'message', message: 'Near enough.' }),
      event({ kind: 'done' }),
    ])
    render(
      <ChatRail
        client={client}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
    const log = measure(theLog(), { top: 790 })
    fireEvent.scroll(log)

    await send('hello')
    await screen.findByText('Near enough.')

    expect(theLog().scrollTop).toBe(1000)
  })
})

/**
 * Item 2: *"The 'Working' text should make better use of the progress meter, it doesn't tell me
 * what's going on."*
 *
 * Three things are honestly available from what the wire carries today, and none of them invents
 * a figure. **What** it is doing comes from `tool_started.message`, which the contract already
 * describes as a human-readable note — better than mapping tool names to labels here, which would
 * duplicate the backend's wording and drift from it. **That it is alive** comes from the same
 * indeterminate meter the replan rail uses when it has no percentage: movement without a claim.
 * And **how long** comes from a clock, which is the thing that actually helps across a silent
 * stretch — the lesson from the eighteen-second judge call.
 *
 * What is *not* available is progress inside a tool. `ChatEvent` has `tool_started` and
 * `tool_finished` and nothing between, so a chat-initiated discovery is silent for 30+ seconds.
 * That is backend's additive field, and when it lands the meter becomes determinate with no
 * structural change here.
 */
describe('ChatRail while it is working', () => {
  function running(events: readonly ChatEvent[]) {
    const chat = vi.fn(
       
      async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
        for (const item of events) yield item
        await new Promise(() => undefined)
      },
    )
    render(
      <ChatRail
        client={{ chat }}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
  }

  it('says what it is doing rather than that it is doing something', async () => {
    running([event({ kind: 'tool_started', message: 'Searching for camps', tool: 'find_places' })])

    await send('find me somewhere to camp')

    const status = await screen.findByTestId('chat-activity')
    expect(status.textContent ?? '').toMatch(/Searching for camps/)
    expect(status.textContent ?? '').not.toMatch(/^Working/)
  })

  it('falls back to Working before any tool has reported', async () => {
    // A turn that is thinking rather than calling anything has nothing more specific to say, and
    // inventing a label for it would be worse than the honest generic one.
    running([])

    await send('hello')

    expect((await screen.findByTestId('chat-activity')).textContent ?? '').toMatch(/Working/)
  })

  it('moves on to the next thing rather than sticking on the first', async () => {
    running([
      event({ kind: 'tool_started', message: 'Searching for camps', tool: 'find_places' }),
      event({ kind: 'tool_finished', message: 'Found 3 camps', tool: 'find_places' }),
      event({ kind: 'tool_started', message: 'Adding Lone Fir to the route', tool: 'add_poi_to_route' }),
    ])

    await send('add the best one')

    await waitFor(() => {
      expect((screen.getByTestId('chat-activity').textContent ?? '')).toMatch(/Adding Lone Fir/)
    })
  })

  it('shows a meter that moves without claiming a figure', async () => {
    // There is no percentage to report inside a chat turn, so the meter is indeterminate — the
    // same one the replan rail uses when it has no number. A bar at 0% reads as stuck.
    running([event({ kind: 'tool_started', message: 'Searching for camps', tool: 'find_places' })])

    await send('find camps')

    const meter = await screen.findByRole('progressbar', { name: /assistant/i })
    expect(meter).not.toHaveAttribute('aria-valuenow')
  })

  it('stops saying anything once the turn is over', async () => {
    const client = fakeClient([event({ kind: 'message', message: 'Done.' }), event({ kind: 'done' })])
    render(
      <ChatRail
        client={client}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )

    await send('anything else?')
    await screen.findByText('Done.')

    expect(screen.queryByTestId('chat-activity')).not.toBeInTheDocument()
  })
})

describe('ChatRail elapsed time', () => {
  function stall() {
    const chat = vi.fn(
      // eslint-disable-next-line require-yield
      async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
        await new Promise(() => undefined)
      },
    )
    render(
      <ChatRail
        client={{ chat }}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
  }

  it('counts the wait once it is long enough to notice', async () => {
    // The thing that actually helps across a silent stretch: a chat-initiated discovery is 30+
    // seconds with no events at all, and seconds advancing is the only honest signal that
    // anything is still happening.
    vi.useFakeTimers()
    try {
      stall()
      await sendWithTimers('plan me three days')

      await act(async () => {
        vi.setSystemTime(Date.now() + 11_000)
        await vi.advanceTimersByTimeAsync(1000)
      })

      expect(screen.getByTestId('chat-activity').textContent ?? '').toMatch(/12s/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('says nothing about time for a turn that answers quickly', async () => {
    // A counter that flashes up for one second and vanishes is noise, and it makes a fast answer
    // look slow.
    vi.useFakeTimers()
    try {
      stall()
      await sendWithTimers('hello')

      await act(async () => {
        vi.setSystemTime(Date.now() + 1000)
        await vi.advanceTimersByTimeAsync(1000)
      })

      expect(screen.getByTestId('chat-activity').textContent ?? '').not.toMatch(/\ds/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('measures the wait rather than counting ticks', async () => {
    // Browsers throttle intervals hard in a background tab, and a rider who switches away during
    // a three-minute plan is exactly who the counter is for. Same fix as the replan clock.
    vi.useFakeTimers()
    try {
      stall()
      await sendWithTimers('plan me three days')

      await act(async () => {
        vi.setSystemTime(Date.now() + 89_000)
        await vi.advanceTimersByTimeAsync(1000)
      })

      expect(screen.getByTestId('chat-activity').textContent ?? '').toMatch(/1m 30s/)
    } finally {
      vi.useRealTimers()
    }
  })
})

/**
 * Progress inside a tool, which is the part item 2 could not answer until now.
 *
 * Three properties, and the second is the one that would be easy to get wrong by copying the
 * replan rail:
 *
 * **Progress replaces itself; messages accumulate.** That distinction is why backend chose a new
 * kind rather than reusing `message` — twenty lines of "scoring 3/41" left in the transcript
 * after the answer arrives is worse than the silence it replaces.
 *
 * **The fraction is within the tool, not the turn.** So it legitimately goes back to nothing and
 * starts again when a second tool runs. The replan rail's highest-seen rule is right there and
 * wrong here: there, a retreating bar meant events arriving out of order; here it means a new
 * tool.
 *
 * **Null means unknown, not zero.** Not every tool can say, and a bar at 0% reads as stuck.
 */
describe('ChatRail progress inside a tool', () => {
  function turn(events: readonly ChatEvent[]) {
    const chat = vi.fn(
       
      async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
        for (const item of events) yield item
        await new Promise(() => undefined)
      },
    )
    render(
      <ChatRail
        client={{ chat }}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
  }

  it('shows the figure the tool reports', async () => {
    turn([
      event({ kind: 'tool_started', message: 'Searching for camps', tool: 'find_places' }),
      event({ kind: 'tool_progress', message: 'scoring 12/41 places', tool: 'find_places', progress: 0.29 }),
    ])

    await send('find camps')

    const meter = await screen.findByRole('progressbar', { name: /assistant/i })
    await waitFor(() => {
      expect(meter).toHaveAttribute('aria-valuenow', '29')
    })
  })

  it('does not leave the progress notes in the conversation', async () => {
    // The whole reason for a separate kind. The transcript is what was said; progress is what is
    // happening, and only one of those is worth keeping once the answer arrives.
    turn([
      event({ kind: 'tool_progress', message: 'scoring 3/41 places', tool: 'find_places', progress: 0.07 }),
      event({ kind: 'tool_progress', message: 'scoring 12/41 places', tool: 'find_places', progress: 0.29 }),
      event({ kind: 'tool_progress', message: 'scoring 39/41 places', tool: 'find_places', progress: 0.95 }),
    ])

    await send('find camps')
    await waitFor(() => {
      expect(screen.getByTestId('chat-activity').textContent ?? '').toMatch(/39\/41/)
    })

    // One line, replaced three times — not three lines.
    expect(screen.queryAllByText(/scoring \d+\/41/)).toHaveLength(1)
  })

  it('lets the figure start again when a second tool runs', async () => {
    // Within the tool, not the turn. Carrying the highest seen across tools would show a bar
    // stuck near the end of the first tool while the second one is barely started.
    //
    // No `tool_started` between them, deliberately. With one, the reset it performs hides
    // whether the figure itself is highest-seen — I wrote it that way first and a
    // highest-seen mutation passed, because the two are indistinguishable once something else
    // clears the value. The tool changing is the signal being tested here.
    turn([
      event({ kind: 'tool_progress', message: 'scoring 39/41', tool: 'find_places', progress: 0.95 }),
      event({ kind: 'tool_progress', message: 'adding 1/8', tool: 'add_poi_to_route', progress: 0.12 }),
    ])

    await send('add the good ones')

    await waitFor(() => {
      expect(screen.getByRole('progressbar', { name: /assistant/i })).toHaveAttribute(
        'aria-valuenow',
        '12',
      )
    })
  })

  it('goes back to a moving bar when a tool cannot say how far along it is', async () => {
    // Null is unknown, not zero. A determinate bar at 0% reads as stuck, which is the thing the
    // indeterminate sweep exists to avoid.
    //
    // The null arrives on a `tool_progress` rather than via a `tool_started` reset, because the
    // reset would prove nothing about how a null *figure* is read — which is exactly what a
    // surviving `?? 0` mutation showed when this test went through `tool_started`.
    turn([
      event({ kind: 'tool_progress', message: 'scoring 12/41', tool: 'find_places', progress: 0.29 }),
      event({ kind: 'tool_progress', message: 'still working', tool: 'find_places', progress: null }),
    ])

    await send('what have we got')

    await waitFor(() => {
      expect(screen.getByRole('progressbar', { name: /assistant/i })).not.toHaveAttribute(
        'aria-valuenow',
      )
    })
  })

  it('stops claiming a figure once the tool has finished', async () => {
    turn([
      event({ kind: 'tool_progress', message: 'scoring 39/41', tool: 'find_places', progress: 0.95 }),
      event({ kind: 'tool_finished', message: 'Found 29 places', tool: 'find_places' }),
    ])

    await send('find camps')

    await waitFor(() => {
      expect(screen.getByRole('progressbar', { name: /assistant/i })).not.toHaveAttribute(
        'aria-valuenow',
      )
    })
  })
})

describe('every event kind the contract declares', () => {
  /**
   * A tripwire rather than an exhaustive `switch` with a `never` default.
   *
   * The integrator hoped the new kind would break my build. It would have, with a `never`
   * assertion — and I decided against one, because it makes an *additive* contract change red on
   * this side before anything can be done about it. That is the coupling the `Omit` bridge existed
   * to avoid on a field removal, and there is no equivalent trick for a union member.
   *
   * A test gives the same information — it names the kind and points at the file — without
   * blocking backend from landing an additive change. It fails in CI rather than in their build,
   * which is where an unhandled kind is this side's problem to fix.
   */
  it('is handled here', () => {
    const declared = new Set(
      (spec.components.schemas.ChatEvent.properties.kind.enum ?? []),
    )

    expect([...HANDLED_KINDS].sort()).toEqual([...declared].sort())
  })
})

/**
 * What the wire actually carries, as opposed to what the contract permits.
 *
 * Logged against the live stack, 2026-08-26: **`tool_started` and `tool_finished` carry an empty
 * message, always.** The agent constructs both without one. Every test above passes a note on
 * those kinds and every one of them was green, describing a stream that does not exist — so a
 * rider watching `describe_trip` run was told "Working…" and nothing else.
 *
 * The fixes here are the ones that need nothing from the backend. A note on `tool_started` is
 * backend's to add, and when it lands it supersedes the fallback below without a change here.
 */
describe('ChatRail when a tool says nothing about itself', () => {
  function running(events: readonly ChatEvent[]) {
    const chat = vi.fn(
      async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
        for (const item of events) yield item
        await new Promise(() => undefined)
      },
    )
    render(
      <ChatRail
        client={{ chat }}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
  }

  it('names the tool from the wire rather than saying nothing', async () => {
    // Derived from `tool`, not from a table of phrases invented here: a hand-written label would
    // duplicate wording the backend owns and drift from it. This cannot drift, because it is the
    // identifier the backend sent, made readable.
    running([event({ kind: 'tool_started', message: '', tool: 'find_places' })])

    await send('find me somewhere to camp')

    const status = await screen.findByTestId('chat-activity')
    expect(status.textContent ?? '').toMatch(/find places/i)
  })

  it('prefers the tool\u2019s own words the moment it has any', async () => {
    running([event({ kind: 'tool_started', message: 'Searching along the route', tool: 'find_places' })])

    await send('find me somewhere to camp')

    const status = await screen.findByTestId('chat-activity')
    expect(status.textContent ?? '').toMatch(/Searching along the route/)
    expect(status.textContent ?? '').not.toMatch(/find places/i)
  })

  it('stops claiming a tool is running once it has finished', async () => {
    // Measured: two seconds pass between a tool finishing and the model saying anything, and the
    // line used to hold the finished tool's name through all of it.
    // With a note, so this fails for the right reason rather than because nothing was ever set.
    running([
      event({ kind: 'tool_started', message: 'Reading the trip', tool: 'describe_trip' }),
      event({ kind: 'tool_finished', message: '', tool: 'describe_trip' }),
    ])

    await send('what have I got')

    await waitFor(() => {
      expect(screen.getByTestId('chat-activity').textContent ?? '').toMatch(/^Working/)
    })
  })

  it('leaves one line in the transcript for a tool that ran, not two', async () => {
    // The interaction with backend's change, and the reason it is worth landing first: with a
    // note on both kinds, a six-tool turn becomes twelve lines of log — which is the thing Tim
    // asked to have folded away on the replan side. Started is what is happening and belongs to
    // the activity line; finished is what happened and belongs in the transcript.
    running([
      event({ kind: 'tool_started', message: 'Searching along the route', tool: 'find_places' }),
      event({ kind: 'tool_finished', message: 'Found 10 places', tool: 'find_places' }),
    ])

    await send('find me somewhere to camp')

    expect(await screen.findByText('Found 10 places')).toBeInTheDocument()
    expect(screen.queryByText('Searching along the route')).not.toBeInTheDocument()
  })
})

/**
 * How long *this step* has been running.
 *
 * The worst moment in a measured turn was not a missing event: the judge's first call took
 * **16.6 seconds** with the line frozen on "scoring 10 places", because that stage is one model
 * call for the whole batch and has nothing to report until it returns. A turn-wide clock says
 * "27s" there, which answers a question nobody asked. What tells a rider the difference between
 * working and hung is how long the thing on screen has been the thing on screen.
 */
describe('ChatRail timing the step in front of you', () => {
  function stalling(events: readonly ChatEvent[]) {
    const chat = vi.fn(
      async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
        for (const item of events) yield item
        await new Promise(() => undefined)
      },
    )
    render(
      <ChatRail
        client={{ chat }}
        resolveSlug={() => Promise.resolve('wabdr-north')}
        onTripChanged={vi.fn()}
      />,
    )
  }

  it('starts the clock again when the activity changes', async () => {
    vi.useFakeTimers()
    try {
      stalling([event({ kind: 'tool_progress', message: 'scoring 10 places', tool: 'find_places' })])
      await sendWithTimers('find me somewhere to camp')

      await act(async () => {
        vi.setSystemTime(Date.now() + 14_000)
        await vi.advanceTimersByTimeAsync(1000)
      })

      // Fifteen seconds into the turn, and fifteen into this step, so far indistinguishable.
      expect(screen.getByTestId('chat-activity').textContent ?? '').toMatch(/15s/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('times the step rather than the turn', async () => {
    // The distinguishing case, and the one a real turn produced: the turn is over a minute old
    // and the step in front of you is seconds old. A turn-wide clock reads "1m 2s" there and
    // makes a step that just started look like the stall.
    vi.useFakeTimers()
    try {
      // Held open so the second step can arrive *after* the clock has run on, which is the
      // whole point — yielding both up front would make the two figures the same number.
      let release = (): void => undefined
      const gate = new Promise<void>((resolve) => {
        release = resolve
      })
      const chat = vi.fn(
        async function* (_slug: string, _request: ChatRequest, _options?: RequestOptions) {
          yield event({
            kind: 'tool_progress',
            message: 'checking 32 places are real',
            tool: 'find_places',
          })
          await gate
          yield event({ kind: 'tool_progress', message: 'scoring 10 places', tool: 'find_places' })
          await new Promise(() => undefined)
        },
      )
      render(
        <ChatRail
          client={{ chat }}
          resolveSlug={() => Promise.resolve('wabdr-north')}
          onTripChanged={vi.fn()}
        />,
      )
      await sendWithTimers('find me somewhere to camp')

      await act(async () => {
        vi.setSystemTime(Date.now() + 61_000)
        await vi.advanceTimersByTimeAsync(1000)
      })
      // A minute in, on the old step, and the line says so.
      expect(screen.getByTestId('chat-activity').textContent ?? '').toMatch(/1m/)

      // Let the new step land first, then let the clock tick underneath it. Combined into one
      // advance, a mutant that merely zeroes the counter without restarting the clock survives:
      // the display is reset and no tick happens afterwards to expose the stale baseline.
      await act(async () => {
        release()
        await Promise.resolve()
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000)
      })

      const shown = screen.getByTestId('chat-activity').textContent ?? ''
      expect(shown).toMatch(/scoring 10 places/)
      // The turn is still over a minute old. This step is not.
      expect(shown).not.toMatch(/1m/)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('ChatRail composer placeholder', () => {
  /**
   * The example teaches a rider what they can type here. Once they have typed something it is
   * a suggestion for a thing they have already done, so it goes.
   *
   * Nothing queries the field by its placeholder text, here or anywhere else in the suite —
   * checked, because a test that found the textarea that way could not then assert the
   * placeholder was gone: it would have nothing left to search by. The visible
   * `<span class="chat__label">` inside the `<label>` is what names the field, and it is what
   * every test uses.
   */
  it('offers an example before the rider has said anything', () => {
    render(<ChatRail client={fakeClient([])} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    // The copy pinned literally rather than matched loosely. It is rider-facing wording that
    // was specified, so a test is the right place for it to be written down.
    expect(screen.getByRole('textbox', { name: /ask the assistant/i })).toHaveAttribute(
      'placeholder',
      'Three days of dirt out of Leavenworth…',
    )
  })

  it('leaves an empty box once the conversation has started, still named', async () => {
    const client = fakeClient([
      event({ kind: 'message', message: 'Three days it is.' }),
      event({ kind: 'done' }),
    ])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('three days of dirt')

    expect(await screen.findByText('Three days it is.')).toBeInTheDocument()
    // Found by its accessible name with no placeholder present, which is the whole safety
    // argument: the label names the field, so removing the placeholder cannot leave an
    // unlabelled input. Asserting the name and the absence together is what proves it.
    const field = screen.getByRole('textbox', { name: /ask the assistant/i })
    expect(field).not.toHaveAttribute('placeholder')
  })

  it('stays empty after a turn that only produced a failure', async () => {
    // The conversation has started even when the assistant never answered. `entries` is the
    // right test for that because the rider's own line is appended the moment they send,
    // before anything comes back.
    const client = fakeClient([
      event({ kind: 'tool_failed', tool: 'add_place', message: 'no such place' }),
      event({ kind: 'done' }),
    ])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('add somewhere that is not there')

    expect(await screen.findByRole('alert')).toHaveTextContent(/no such place/)
    expect(screen.getByRole('textbox', { name: /ask the assistant/i })).not.toHaveAttribute(
      'placeholder',
    )
  })
})
