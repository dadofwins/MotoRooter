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
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
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

/**
 * How close to the bottom still counts as following.
 *
 * Not an exact comparison. Rounding, momentum scrolling and sub-pixel line heights mean a log a
 * rider is sitting at the bottom of routinely reports a few pixels short, and an exact test would
 * silently stop following after one flick of a trackpad.
 */
const STUCK_TO_BOTTOM_PX = 24

/**
 * How long a turn has to run before the wait is worth naming.
 *
 * A counter that flashes up for one second and vanishes is noise, and it makes a fast answer look
 * slow. Below this the meter alone is enough.
 */
const ELAPSED_AFTER_S = 3

/** Minutes and seconds, so nobody has to divide their own wait. Matches the replan rail. */
function formatWait(seconds: number): string {
  if (seconds < 60) return `${String(seconds)}s`
  return `${String(Math.floor(seconds / 60))}m ${String(seconds % 60)}s`
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
  /**
   * What the assistant is doing right now.
   *
   * The note off the most recent tool event rather than a label derived from `tool`. The contract
   * already describes `message` on a tool event as a human-readable note, so mapping tool names
   * to words here would duplicate the backend's wording and drift from it the first time either
   * side changed one.
   */
  const [activity, setActivity] = useState<string | null>(null)
  /**
   * How far through the *current tool* the run is, or null when nothing can say.
   *
   * Not the highest seen, which is the replan rail's rule and would be wrong here: there a
   * retreating figure meant events arriving out of order, here it means a second tool started.
   * Cleared whenever a tool begins or ends, so a bar never describes work that is over.
   */
  const [toolProgress, setToolProgress] = useState<number | null>(null)
  const [elapsedS, setElapsedS] = useState(0)
  /** When this turn began, so the wait is measured rather than counted — see `useReplan`. */
  const startedAt = useRef(0)

  // The interval drives the render; the clock supplies the number. Counting ticks would make the
  // figure lie in a background tab, which is exactly where a rider waiting three minutes goes.
  useEffect(() => {
    if (!isRunning) return undefined
    const timer = setInterval(() => {
      setElapsedS(Math.round((Date.now() - startedAt.current) / 1000))
    }, 1000)
    return () => {
      clearInterval(timer)
    }
  }, [isRunning])

  const logRef = useRef<HTMLDivElement | null>(null)
  /**
   * Whether the rider is following the conversation or reading back through it.
   *
   * A ref rather than state: it changes on every scroll event and nothing renders differently for
   * it, so making it state would re-render the transcript while someone is scrolling through it.
   * Starts true, because a fresh rail is at the bottom by definition.
   */
  const following = useRef(true)

  // After the DOM has the new entry and before the browser paints, so the transcript never
  // appears at the old position for a frame.
  useLayoutEffect(() => {
    const log = logRef.current
    if (log === null || !following.current) return
    log.scrollTop = log.scrollHeight
  }, [entries, isRunning, truncated])

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
      setActivity(null)
      setToolProgress(null)
      startedAt.current = Date.now()
      setElapsedS(0)
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
            // The current activity, and also kept in the transcript: one is what is happening,
            // the other is what happened.
            if (item.message !== '') setActivity(item.message)
            if (item.message !== '') append('tool', item.message)
            // A new tool has its own scale, and does not inherit the last one's figure.
            setToolProgress(null)
            break
          case 'tool_progress':
            // Replaces itself and never reaches the transcript. That distinction is why this is
            // a separate kind: twenty lines of "scoring 3/41" sitting under the answer is worse
            // than the silence it replaces.
            if (item.message !== '') setActivity(item.message)
            // Null is unknown rather than zero — not every tool can say, and a determinate bar
            // at nothing reads as stuck.
            setToolProgress(item.progress ?? null)
            break
          case 'tool_finished':
            setToolProgress(null)
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
          setActivity(null)
        },
        (reason: unknown) => {
          if (isAbortError(reason) || controller.signal.aborted) return
          running.current = null
          setRunning(false)
          setActivity(null)
          const failure = failureText(reason)
          append(failure.kind, failure.text)
        },
      )
    },
    [append, client, isRunning, onTripChanged, resolveSlug],
  )

  return (
    <div className="chat">
      <div
        className="chat__log"
        ref={logRef}
        onScroll={(scrolled) => {
          // Leaving the bottom stops the following; returning resumes it. No separate control, and
          // no state that can get stuck in the wrong position.
          const log = scrolled.currentTarget
          following.current =
            log.scrollHeight - log.scrollTop - log.clientHeight <= STUCK_TO_BOTTOM_PX
        }}
      >
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
          <div className="chat__activity">
            <p className="chat__working" role="status" data-testid="chat-activity">
              {activity ?? 'Working'}&hellip;
              {elapsedS >= ELAPSED_AFTER_S && ` · ${formatWait(elapsedS)}`}
            </p>
            {/* Indeterminate, because there is no figure to report: `ChatEvent` has
                `tool_started` and `tool_finished` and nothing between, so a chat-initiated
                discovery is genuinely silent for 30+ seconds. The sweep says working without
                claiming progress it has not made — the same choice as the replan meter before
                it has a percentage, and it becomes determinate with no structural change here
                once backend adds progress inside a turn. */}
            {/* Determinate where a tool reports a figure, indeterminate where none does. Reaching
                the end means *that tool* finished rather than the assistant being done, which is
                why it can drop back to a sweep and start again. */}
            <div className="progress__meter">
              <div
                className={[
                  'progress__bar',
                  toolProgress === null ? 'progress__bar--indeterminate' : '',
                  'progress__bar--working',
                ]
                  .filter(Boolean)
                  .join(' ')}
                role="progressbar"
                aria-label="Assistant working"
                {...(toolProgress === null
                  ? {}
                  : {
                      'aria-valuenow': Math.round(toolProgress * 100),
                      'aria-valuemin': 0,
                      'aria-valuemax': 100,
                      style: { width: `${String(Math.round(toolProgress * 100))}%` },
                    })}
              />
            </div>
          </div>
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
