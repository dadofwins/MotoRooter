import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ChatRail } from './ChatRail'
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

  it('names the tool it is using while it is using it', async () => {
    // The slow path can take a while, and "thinking…" for twenty seconds is indistinguishable
    // from a hang. Saying which tool is running is the difference.
    const client = fakeClient([
      event({ kind: 'tool_started', message: 'Searching for camps', tool: 'find_camps' }),
      event({ kind: 'tool_finished', message: 'Found 3 camps', tool: 'find_camps' }),
      event({ kind: 'done' }),
    ])
    render(<ChatRail client={client} resolveSlug={() => Promise.resolve('wabdr-north')} onTripChanged={vi.fn()} />)

    await send('find camps')

    expect(await screen.findByText(/Searching for camps/)).toBeInTheDocument()
    expect(await screen.findByText(/Found 3 camps/)).toBeInTheDocument()
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
