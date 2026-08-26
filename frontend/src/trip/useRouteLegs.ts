/**
 * Routes each leg of a trip, independently.
 *
 * A trip used to be one leg spanning every waypoint, so every edit re-routed everything. Now
 * the leg structure comes in from the caller (see `legStructure`) and this hook answers one
 * question per leg: does this leg's geometry still match its waypoints and intent, and if not,
 * what does the router say.
 *
 * Three properties make it behave on a metered provider, and each of them is a bug that was
 * easy to write instead:
 *
 * - **A leg is asked about once.** Answers are keyed on the *question* — the leg's waypoints,
 *   intent and provider override — not on the array holding it. So a parent re-deriving legs
 *   every render costs nothing, and appending a waypoint costs exactly one request.
 * - **Content keys make out-of-order responses harmless.** The classic guard is a monotonic
 *   sequence number, because a slow early response landing last silently reverts a newer edit.
 *   With several legs in flight at once that is fiddly; keying the answer by the question
 *   removes the failure instead of policing it — a response can only fill the slot it was
 *   asked for.
 * - **Only superseded requests are aborted.** Cancelling everything on any change is the
 *   tempting one-liner and it doubles the request count: appending a waypoint would abort the
 *   in-flight leg before it, then immediately ask again for the same thing.
 *
 * Partial failure is deliberate. One unroutable segment leaves every other segment drawn, and
 * the failure count is reported so the rail can say so — the same argument the backend settled
 * on the discovery resolver.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ApiClient } from '../api/client'
import { isLegStale, legWaypoints } from '../routing/tripEdits'
import type { Coordinate, RouteLeg, TripLeg, Waypoint } from '../api/types'

/** Only the one call is needed, so a test double stays a one-line object. */
export type LegRouter = Pick<ApiClient, 'routeLeg'>

/**
 * How many leg answers to keep.
 *
 * Legs rather than whole routes, so this is generous: an editing session on a ten-leg trip
 * with plenty of undo stays inside it, and a long one still cannot grow without bound.
 */
const MAX_CACHED_LEGS = 200

/** What came back for one leg. */
interface Answered {
  readonly routed: RouteLeg
  /**
   * Riding time for this leg, in seconds.
   *
   * From `RouteLegResponse.estimated_duration_s`, derived server-side from distance and
   * surface. Never `RouteLeg.duration_s`: hosted ORS routes dirt through a bicycle profile and
   * reported 2.31 h for a 39 km leg that takes about 45 minutes.
   */
  readonly estimatedDurationS: number | null
}

/** A leg, the question it asks, and whether it still needs asking. */
interface Asked {
  readonly leg: TripLeg
  readonly coordinates: readonly Coordinate[]
  readonly question: string
  readonly stale: boolean
}

export interface RouteLegsState {
  /** The structure it was given, with geometry filled in wherever it is known. */
  readonly legs: readonly TripLeg[]
  /**
   * Riding time for the whole trip, in seconds, or `null` when it cannot be totalled.
   *
   * Null rather than a partial sum. A trip half of whose legs have routed would otherwise
   * report half a day as the whole day, which is precisely the number a rider plans around —
   * and legs restored from storage carry no per-leg estimate at all, so the partial sum for a
   * loaded three-day trip would be however much of it the rider has just re-routed. When this
   * is null the caller falls back to the trip document's own figure.
   */
  readonly estimatedDurationS: number | null
  /** Any leg still in flight. */
  readonly isRouting: boolean
  /** One of the current failures, for the message. Null when nothing current has failed. */
  readonly error: Error | null
  /** How many of the current legs the router refused. */
  readonly unroutableCount: number
}

/** Identifies the question a leg asks: where to, how, and through which engine. */
function questionFor(coordinates: readonly Coordinate[], leg: TripLeg): string {
  const points = coordinates.map((point) => `${String(point.lat)},${String(point.lon)}`).join(';')
  return `${leg.intent}|${leg.provider_override ?? ''}|${points}`
}

export function useRouteLegs(
  client: LegRouter,
  waypoints: readonly Waypoint[],
  legs: readonly TripLeg[],
): RouteLegsState {
  const [answers, setAnswers] = useState<ReadonlyMap<string, Answered>>(() => new Map())
  const [failures, setFailures] = useState<ReadonlyMap<string, Error>>(() => new Map())

  /**
   * Each leg with its question.
   *
   * A leg arriving with geometry from a drag is not stale, so it is never re-requested — the
   * drag already paid for that answer. Freshness comes from `RouteLeg.routed_from` rather than
   * from who called last, which is what makes it safe regardless of ordering.
   */
  const asked = useMemo<readonly Asked[]>(
    () =>
      legs.map((leg) => {
        const coordinates = legWaypoints(waypoints, leg)
        return {
          leg,
          coordinates,
          question: questionFor(coordinates, leg),
          stale: isLegStale(waypoints, leg),
        }
      }),
    [legs, waypoints],
  )

  /** Identifies the whole trip by what it asks, so a re-derived array is not a change. */
  const structureKey = asked.map((entry) => entry.question).join('||')

  // Read inside the effect rather than depended on. Depending on the answers means every one
  // re-runs the effect, and each re-run re-fires the legs that failed — a wasted request per
  // sibling success, on exactly the trips that are already going badly.
  const held = useRef(answers)
  const pending = useRef(asked)
  useEffect(() => {
    held.current = answers
    pending.current = asked
  }, [answers, asked])

  /** In-flight requests by question, so a leg is never asked about twice at once. */
  const inFlight = useRef(new Map<string, AbortController>())

  useEffect(() => {
    const wanted = new Set(
      pending.current.filter((entry) => entry.stale).map((entry) => entry.question),
    )

    // Superseded, not merely older: a request whose leg is no longer part of the trip. One
    // that is still wanted keeps running, because aborting and re-firing it is two requests
    // for one answer.
    for (const [question, controller] of inFlight.current) {
      if (wanted.has(question)) continue
      controller.abort()
      inFlight.current.delete(question)
    }

    for (const entry of pending.current) {
      if (!entry.stale) continue
      if (entry.coordinates.length < 2) continue // nothing to route between
      if (held.current.has(entry.question)) continue // already answered
      if (inFlight.current.has(entry.question)) continue // already asked

      const controller = new AbortController()
      inFlight.current.set(entry.question, controller)
      const override = entry.leg.provider_override ?? null

      client
        .routeLeg(
          {
            waypoints: [...entry.coordinates],
            intent: entry.leg.intent,
            ...(override === null ? {} : { provider_override: override }),
          },
          { signal: controller.signal },
        )
        .then(
          (response) => {
            inFlight.current.delete(entry.question)
            if (controller.signal.aborted) return
            setAnswers((previous) =>
              remember(previous, entry.question, {
                routed: response.leg,
                estimatedDurationS: response.estimated_duration_s,
              }),
            )
          },
          (reason: unknown) => {
            inFlight.current.delete(entry.question)
            if (controller.signal.aborted) return
            // Recorded against the question, so it is forgotten the moment the rider changes
            // the leg — and asked again if they come back to it.
            setFailures((previous) =>
              new Map(previous).set(
                entry.question,
                reason instanceof Error ? reason : new Error(String(reason)),
              ),
            )
          },
        )
    }
  }, [client, structureKey])

  // Everything in flight is worthless once there is nobody to deliver it to.
  useEffect(() => {
    const running = inFlight.current
    return () => {
      for (const controller of running.values()) controller.abort()
      running.clear()
    }
  }, [])

  return useMemo(() => {
    // A stale leg keeps whatever geometry it arrived with until its answer lands. `insertVia`
    // relies on that: blanking the segment being dragged would make the route come apart on
    // every edit, and a leg whose shape genuinely changed arrives with `routed: null` already.
    const filled = asked.map((entry) => {
      const answer = entry.stale ? answers.get(entry.question) : undefined
      return answer === undefined ? entry.leg : { ...entry.leg, routed: answer.routed }
    })

    const estimates = asked.map((entry) =>
      entry.stale ? (answers.get(entry.question)?.estimatedDurationS ?? null) : null,
    )
    const totalled =
      estimates.length > 0 && estimates.every((seconds) => seconds !== null)
        ? estimates.reduce((total, seconds) => total + (seconds ?? 0), 0)
        : null

    // Only the current legs' failures. Otherwise deleting the segment that failed leaves a
    // message on screen that cannot be dismissed.
    const failed = asked.filter((entry) => entry.stale && failures.has(entry.question))
    const stillAsking = asked.some(
      (entry) =>
        entry.stale &&
        entry.coordinates.length >= 2 &&
        !answers.has(entry.question) &&
        !failures.has(entry.question),
    )

    return {
      legs: filled,
      estimatedDurationS: totalled,
      isRouting: stillAsking,
      error: failed[0] === undefined ? null : (failures.get(failed[0].question) ?? null),
      unroutableCount: failed.length,
    }
  }, [asked, answers, failures])
}

/** A copy of the answers with one added, evicting the oldest once it is full. */
function remember(
  answers: ReadonlyMap<string, Answered>,
  question: string,
  answer: Answered,
): ReadonlyMap<string, Answered> {
  const next = new Map(answers)
  next.set(question, answer)
  if (next.size > MAX_CACHED_LEGS) {
    const oldest = next.keys().next()
    if (!oldest.done) next.delete(oldest.value)
  }
  return next
}
