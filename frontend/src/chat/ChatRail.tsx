/**
 * The assistant rail.
 *
 * **Chat is an accelerator, never a requirement.** Everything the assistant can do is a second
 * path to something the mouse already does, so this component deliberately owns no trip state.
 * Its job is to say what the assistant is doing, and to tell the app when the trip changed
 * underneath it.
 *
 * That last part is the load-bearing decision. `trip_changed` says *that* the document changed,
 * not how, and the rail re-reads rather than replaying events into local state. Replaying would
 * make two models of one trip — the mouse's and the chat's — and they would diverge silently,
 * which is exactly the failure the single-service-function rule exists to prevent.
 *
 * The transcript lives here because the server is stateless: `ChatRequest.history` carries it,
 * which makes "what did the assistant see" answerable from the request alone.
 *
 * The distinctions worth building carefully are all about a turn that did not finish cleanly. A
 * rider waiting needs to tell working from finished from cut off, and all three look identical
 * if the rail only shows text.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ApiClient } from '../api/client'
import { isAbortError, isApiError, isNotImplemented } from '../api/errors'
import type { ChatEvent, ChatTurn } from '../api/types'

export type ChatClient = Pick<ApiClient, 'chat'>

export interface ChatRailProps {
  readonly client: ChatClient
  /**
   * The slug of a trip to talk about, creating one if there is none yet.
   *
   * A function rather than a value because the opening line invites the rider to describe a
   * trip *before* placing a point. Handing the rail a nullable slug meant disabling the input
   * until a trip was saved, which makes the app's own first sentence unreachable.
   */
  readonly resolveSlug: () => Promise<string>
  /** The assistant edited the trip. Re-read it; do not reconstruct it from the stream. */
  readonly onTripChanged: () => void
}

/** One line in the transcript, as the rider sees it. */
interface Entry {
  readonly id: number
  readonly kind: 'you' | 'assistant' | 'tool' | 'tool-failed' | 'unavailable' | 'error'
  readonly text: string
}

/** What the rider is told when a turn cannot run at all. */
const UNREACHABLE = 'The assistant could not be reached. Check your connection and try again.'
const NOT_BUILT = 'The assistant is not built yet — this is coming soon.'

function failureText(reason: unknown): { kind: Entry['kind']; text: string } {
  // 501 is not a failure, it is a promise. Presenting it as an error trains a rider to
  // distrust the rail once it does work.
  if (isNotImplemented(reason)) return { kind: 'unavailable', text: NOT_BUILT }
  // Never `reason.message`: an internal string would make a dropped connection and a server
  // bug read identically.
  if (isApiError(reason) && reason.code === 'rate_limited') {
    return { kind: 'error', text: 'Too many requests just now. Give it a moment and try again.' }
  }
  return { kind: 'error', text: UNREACHABLE }
}

export function ChatRail({ client, resolveSlug, onTripChanged }: ChatRailProps): React.JSX.Element {
  const [entries, setEntries] = useState<readonly Entry[]>([])
  const [draft, setDraft] = useState('')
  const [isRunning, setRunning] = useState(false)
  const [truncated, setTruncated] = useState(false)

  /** The conversation as the assistant will be shown it. Oldest first. */
  const history = useRef<readonly ChatTurn[]>([])
  const nextId = useRef(0)
  const running = useRef<AbortController | null>(null)

  // A turn outliving its rail delivers events into a dead tree, and on the way it spends an
  // OpenAI call plus whatever tools the assistant decides to run.
  useEffect(
    () => () => {
      running.current?.abort()
      running.current = null
    },
    [],
  )

  /** Allocated outside every state updater, so the updaters stay pure. */
  const append = useCallback((kind: Entry['kind'], text: string) => {
    const entry: Entry = { id: nextId.current++, kind, text }
    setEntries((previous) => [...previous, entry])
  }, [])

  const send = useCallback(
    (message: string) => {
      // One turn at a time. The transcript is the client's and two turns in flight would race
      // to append to it — and the assistant would see a conversation neither of them had.
      if (isRunning || message === '') return

      const controller = new AbortController()
      running.current = controller
      setRunning(true)
      setTruncated(false)
      setDraft('')
      append('you', message)

      const asked: readonly ChatTurn[] = [...history.current, { role: 'user', content: message }]

      const run = async (): Promise<void> => {
        // Before the turn, not during it: the endpoint is addressed by slug, so there has to
        // be a document to address.
        const slug = await resolveSlug()
        const answers: string[] = []

        for await (const item of client.chat(
          slug,
          { message, history: [...history.current] },
          { signal: controller.signal },
        )) {
          if (controller.signal.aborted) return
          applyEvent(item, answers)
        }

        // Recorded only once the turn is over, and only what the assistant actually said —
        // tool notes are for the rider to watch, not context for the next question.
        history.current = [...asked, { role: 'assistant', content: answers.join('\n') }]
      }

      const applyEvent = (item: ChatEvent, answers: string[]): void => {
        switch (item.kind) {
          case 'message':
            if (item.message !== '') {
              answers.push(item.message)
              append('assistant', item.message)
            }
            break
          case 'tool_started':
          case 'tool_finished':
            // Named while it runs. "Thinking…" for twenty seconds is indistinguishable from a
            // hang, and discovery genuinely takes that long.
            if (item.message !== '') append('tool', item.message)
            break
          case 'tool_failed':
            // Kept distinct rather than folded into the answer: a tool that failed changed
            // nothing, and a rider who cannot see that will believe it did.
            if (item.message !== '') append('tool-failed', item.message)
            break
          case 'done':
            if (item.truncated === true) setTruncated(true)
            break
        }
        // Any event may carry it, including the terminal one.
        if (item.trip_changed === true) onTripChanged()
      }

      run().then(
        () => {
          if (controller.signal.aborted) return
          running.current = null
          setRunning(false)
        },
        (reason: unknown) => {
          if (isAbortError(reason) || controller.signal.aborted) return
          running.current = null
          setRunning(false)
          const failure = failureText(reason)
          append(failure.kind, failure.text)
        },
      )
    },
    [append, client, isRunning, onTripChanged, resolveSlug],
  )

  return (
    <div className="chat">
      <div className="chat__log">
        {/* The opening state, specified rather than invented: it has to name both ways in, or
            the rail reads as the only way to start. */}
        <p className="chat__greeting">
          Describe your trip and I&rsquo;ll help plan it for you! Or set a start and end point on
          the map.
        </p>

        {entries.map((entry) => (
          <p
            key={entry.id}
            className={`chat__entry chat__entry--${entry.kind}`}
            // Failures are announced; the rest is read in place. Announcing every tool note
            // would talk over a screen-reader user for the length of a discovery run.
            {...(entry.kind === 'tool-failed' || entry.kind === 'error'
              ? { role: 'alert' as const }
              : {})}
          >
            {entry.text}
          </p>
        ))}

        {isRunning && (
          <p className="chat__working" role="status">
            Working…
          </p>
        )}

        {truncated && (
          // `truncated` rides on the terminal event for exactly this reason: someone waiting
          // needs to know the assistant stopped mid-task rather than having nothing left to say.
          <p className="chat__truncated" role="status">
            That answer was cut off before the assistant finished. Ask again to carry on.
          </p>
        )}
      </div>

      <form
        className="chat__composer"
        onSubmit={(submitted) => {
          submitted.preventDefault()
          send(draft.trim())
        }}
      >
        <label className="chat__field">
          <span className="chat__label">Ask the assistant</span>
          <textarea
            value={draft}
            rows={2}
            placeholder="Three days of dirt out of Leavenworth…"
            onChange={(changed) => setDraft(changed.target.value)}
            onKeyDown={(pressed) => {
              // Enter sends, shift+enter breaks the line: this is a message box, not a document.
              if (pressed.key === 'Enter' && !pressed.shiftKey) {
                pressed.preventDefault()
                send(draft.trim())
              }
            }}
          />
        </label>
        <button type="submit" disabled={isRunning}>
          Send
        </button>
      </form>
    </div>
  )
}
