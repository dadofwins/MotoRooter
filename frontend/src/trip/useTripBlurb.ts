/**
 * The rail header's line about the trip in front of the rider.
 *
 * Two rules govern everything here, and both are about what this must *not* do.
 *
 * **Nothing waits on it.** It is a model call decorating a component the fast path renders
 * through, so it never blocks, never reports, and has no error state. Nothing a rider can see
 * ever goes wrong; what they are looking at simply does not change.
 *
 * Two outcomes rather than one, and the distinction is deliberate. A *failure* — a 501, a
 * dropped connection, a model that fell over — leaves whatever line is on screen alone,
 * because that line is still about their trip and a header that empties on a bad connection
 * is a rider watching something break. A *null answer* takes the line down, because null is
 * the contract's way of saying there is nothing to say about the trip as it now is, and
 * leaving the old line standing over a changed trip is the feature lying. So null is not
 * routed through the catch: it is an answer, and treating it as a failure would both invert
 * that and turn the endpoint's own fallback into an error path.
 *
 * With no line yet, the two coincide: the static header stands either way.
 *
 * **It is not regenerated on every edit.** A drag emits route updates continuously, and one
 * model call per update is exactly the quota failure the drag throttle exists to prevent,
 * arriving through a new door. The protection is structural rather than tuned: this hook is
 * handed the *committed* trip document, never the live edited one, so preview geometry has no
 * path in at all — `App` keeps mid-drag geometry in a separate `preview` state and only
 * `onCommit` writes into the edit. `tripBlurbKey` then decides whether two committed contents
 * are the same trip. Hand this `shownLegs` rather than `legs` and the guarantee is gone; that
 * is the one way to misuse it.
 */
import { useEffect, useRef, useState } from 'react'
import { tripBlurbKey, type BlurbInput } from './tripBlurbKey'
import type { ApiClient } from '../api/client'
import type { ChatTurn } from '../api/types'

export type BlurbClient = Pick<ApiClient, 'tripBlurb'>

/**
 * The conversation so far, read at the moment of asking.
 *
 * A getter rather than the turns themselves, and that is the whole design. History colours a
 * blurb but must never *buy* one: a rider chatting without touching their trip would
 * otherwise spend a model call per turn, which is the churn just taken out of the POI key
 * arriving through the rail instead. Passing a function leaves no array identity for an
 * effect to notice, so the rule is enforced by there being nothing to depend on rather than
 * by remembering to leave something out of a dependency list.
 *
 * It also keeps the transcript where it belongs. `ChatRail` remains its owner and its only
 * writer; this reads the latest value at the one instant it is wanted, which is what the
 * endpoint means by history being optional colour rather than input.
 */
export type RecentTurns = () => readonly ChatTurn[]

const NO_TURNS: readonly ChatTurn[] = []

export function useTripBlurb(
  client: BlurbClient,
  slug: string | null,
  trip: BlurbInput | null,
  recentTurns: RecentTurns = () => NO_TURNS,
): string | null {
  const [blurb, setBlurb] = useState<string | null>(null)

  /**
   * The client, held so its *identity* cannot trigger a request.
   *
   * `App` takes the client as an injectable prop, so nothing here can assume a caller passes
   * a stable object — and this codebase has already lost a whole drag gesture to a helper
   * object rebuilt every render invalidating something keyed on it. Putting `client` in the
   * dependency array would spend a model call per render for a caller who constructs one
   * inline, which is the exact failure this hook exists to avoid.
   *
   * Worse than wasteful, in fact: with cleanup aborting the previous request, a client
   * identity change mid-flight would cancel a request in progress and, if the effect then
   * declined to re-issue it, leave the header with no line at all and nothing pending.
   * Keying on what was *asked* rather than on who asks it removes both.
   */
  const latestClient = useRef(client)
  useEffect(() => {
    latestClient.current = client
  })

  // Same treatment, same reason: read when a request fires, never depended upon. A caller
  // writing this inline hands over a new closure every render, and that must not be an event.
  const latestTurns = useRef(recentTurns)
  useEffect(() => {
    latestTurns.current = recentTurns
  })

  // Nothing to describe: no document, or one with nowhere on it yet. The static greeting is
  // the better line there anyway — it names the map path, which a rider who has placed no
  // points is the one person who still needs.
  const describable = trip !== null && trip.waypoints.length > 0 && slug !== null
  const key = describable ? tripBlurbKey(trip) : null

  useEffect(() => {
    if (key === null || slug === null) return undefined

    const controller = new AbortController()
    // Sampled here, inside the effect, which is the only place the conversation is ever read.
    // Omitted rather than sent empty when there is none: chat is an accelerator and never a
    // requirement, so a rider who has not opened the rail is the ordinary case and an empty
    // array would claim a transcript that does not exist. The backend truncates to its own
    // `MAX_HISTORY_TURNS`, so nothing here has to.
    const turns = latestTurns.current()
    const request = turns.length === 0 ? {} : { history: [...turns] }

    // Wrapped so a *synchronous* throw lands in the same place as a rejection. `.catch` only
    // sees rejections, and this call must have exactly one failure outcome: no line. A
    // decoration that can take the rail down with it is worse than no decoration.
    void (async () =>
      latestClient.current.tripBlurb(slug, request, { signal: controller.signal }))()
      .then((response) => {
        // The stale-response rule the drag path already lives by. A slow first reply landing
        // after a fast second one would describe a trip the rider has moved on from — and it
        // is the cleanup's abort that marks it, so this holds even for a client that ignores
        // the signal it was handed.
        if (controller.signal.aborted) return
        // Null lands here rather than in the catch, because it is an answer. Keeping the
        // previous line would be the wrong reading: the backend has said it has nothing to
        // say about the trip as it now is.
        setBlurb(response.blurb)
      })
      .catch(() => {
        // Every failure is the same failure. Deliberately keeps whatever line is showing
        // rather than clearing it: a header that empties on a dropped connection is a rider
        // watching something break, and the old line is still about their trip.
      })

    return () => {
      controller.abort()
    }
    // Deliberately not `client` — see `latestClient` above. The pair that decides whether a
    // question is worth asking is which trip, and what shape it is in.
  }, [slug, key])

  return describable ? blurb : null
}
