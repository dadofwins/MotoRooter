/**
 * The slow path: discovery and enrichment, streamed.
 *
 * Explicitly user-triggered and never fired by a route edit — that separation is the whole
 * point of having two speeds. Discovery takes tens of seconds, so the run streams and pins
 * appear as they resolve: a rider watching an empty map for thirty seconds concludes the app
 * is broken, which is exactly what streaming exists to prevent.
 *
 * The run holds no lock on anything. Dragging during a replan keeps working, because the fast
 * path never waits on this.
 *
 * The contract's subtlety is that `pois` is cumulative **per stage**, not overall: a later
 * stage's list restarts from its own beginning. Replacing wholesale loses what discovery found
 * when enrichment reports; appending duplicates within a stage. So each stage's latest list is
 * kept separately and the total is their union.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ApiClient } from '../api/client'
import type { Poi, ReplanEvent, Trip, TripLeg } from '../api/types'

export type Replanner = Pick<ApiClient, 'replan'>

/** One thing the run has done, newest first in the log. */
export interface ReplanStep {
  readonly id: number
  readonly message: string
  readonly stage: string
}

export interface ReplanState {
  readonly isRunning: boolean
  /** Which stage the run is in: route_search, discovery, enrichment, done. */
  readonly stage: string | null
  readonly message: string
  readonly progress: number | null
  /** Everything found so far, across every stage. */
  readonly pois: readonly Poi[]
  /** Legs any stage re-routed. Empty until one does. */
  readonly legs: readonly TripLeg[]
  /**
   * What the run has done, newest first.
   *
   * A rider watching a multi-minute operation needs to see it accumulate — "I have no idea
   * what it's doing" was the complaint. Bounded, because parallel discovery will emit far more
   * events than a rail can hold, and consecutive duplicates are collapsed for the same reason.
   */
  readonly log: readonly ReplanStep[]
  /** Whole seconds since the run began, and frozen once it ends. */
  readonly elapsedS: number
  /** A finished run that turned up nothing, which is a real outcome and often today's. */
  readonly foundNothing: boolean
  readonly error: Error | null
  readonly start: (slug: string) => void
  readonly cancel: () => void
}

/**
 * Whether discovery is stale relative to the route.
 *
 * Mirrors `Trip.needs_replan` on the backend — `planned_at is None or edited_at > planned_at`
 * — because that field is serialised on `TripSummary` and not on `Trip`. Derived rather than
 * omitted: stale suggestions a rider cannot detect are worse than no suggestions, which is
 * the reason the flag exists at all.
 *
 * Parsed rather than compared as strings: two ISO-8601 timestamps only sort lexicographically
 * while their formats match exactly, and nothing guarantees that across a schema change.
 */
export function needsReplan(trip: Trip | null): boolean {
  if (trip === null) return false
  if (trip.planned_at === null || trip.planned_at === undefined) return true
  return Date.parse(trip.edited_at) > Date.parse(trip.planned_at)
}

/** Enough to show the shape of what happened without becoming a feed that scrolls away. */
const MAX_LOG_STEPS = 12

export function useReplan(client: Replanner): ReplanState {
  const [isRunning, setRunning] = useState(false)
  const [stage, setStage] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [progress, setProgress] = useState<number | null>(null)
  const [byStage, setByStage] = useState<ReadonlyMap<string, readonly Poi[]>>(new Map())
  const [legs, setLegs] = useState<readonly TripLeg[]>([])
  const [finished, setFinished] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [log, setLog] = useState<readonly ReplanStep[]>([])
  const [elapsedS, setElapsed] = useState(0)
  const nextId = useRef(0)

  const running = useRef<AbortController | null>(null)

  const stop = useCallback(() => {
    running.current?.abort()
    running.current = null
  }, [])

  // A run outliving its component would deliver events into a dead tree.
  useEffect(() => stop, [stop])

  // Counted rather than measured from a clock: a tick is what the display needs, and an
  // interval is what a test can drive deterministically.
  useEffect(() => {
    if (!isRunning) return undefined
    const timer = setInterval(() => {
      setElapsed((previous) => previous + 1)
    }, 1000)
    return () => {
      clearInterval(timer)
    }
  }, [isRunning])

  const start = useCallback(
    (slug: string) => {
      // One run at a time: two would interleave stages and their per-stage lists would fight.
      if (running.current !== null) return

      const controller = new AbortController()
      running.current = controller
      setRunning(true)
      setFinished(false)
      setError(null)
      setByStage(new Map())
      setLegs([])
      setProgress(null)
      setLog([])
      setElapsed(0)

      const consume = async (): Promise<void> => {
        const stream = client.replan(
          slug,
          // Sent explicitly rather than relied upon as a default: a replan that silently
          // discarded hand-placed POIs would be the worst kind of surprise.
          { preserve_pinned: true },
          { signal: controller.signal },
        )
        for await (const item of stream) {
          if (controller.signal.aborted) return
          apply(item)
        }
      }

      const apply = (item: ReplanEvent): void => {
        setStage(item.stage)
        if (item.message !== '') setMessage(item.message)
        // The highest seen, not the latest. Parallel emission means a 40% event can land after
        // a 60% one, and a meter that retreats reads as broken — "at least this far" is both
        // stable and true.
        if (item.progress !== null && item.progress !== undefined) {
          const arrived = item.progress
          setProgress((previous) => (previous === null ? arrived : Math.max(previous, arrived)))
        }
        if (item.message !== '') {
          setLog((previous) => {
            // Consecutive duplicates collapse: parallel steps repeat their wording.
            if (previous[0]?.message === item.message) return previous
            const step: ReplanStep = { id: nextId.current++, message: item.message, stage: item.stage }
            return [step, ...previous].slice(0, MAX_LOG_STEPS)
          })
        }
        if (item.pois !== undefined && item.pois.length > 0) {
          // Replace this stage's contribution, keep every other stage's.
          setByStage((previous) => new Map(previous).set(item.stage, item.pois ?? []))
        }
        if (item.legs !== undefined && item.legs.length > 0) setLegs(item.legs)
      }

      consume().then(
        () => {
          if (controller.signal.aborted) return
          running.current = null
          setRunning(false)
          setFinished(true)
        },
        (reason: unknown) => {
          if (controller.signal.aborted) return
          running.current = null
          setRunning(false)
          setError(reason instanceof Error ? reason : new Error(String(reason)))
        },
      )
    },
    [client],
  )

  const cancel = useCallback(() => {
    stop()
    setRunning(false)
  }, [stop])

  const pois = useMemo(() => {
    // Union across stages, first sighting wins: enrichment re-reporting a POI discovery
    // already found should not produce two pins on the same spot.
    const seen = new Map<string, Poi>()
    for (const found of byStage.values()) {
      for (const poi of found) if (!seen.has(poi.id)) seen.set(poi.id, poi)
    }
    return [...seen.values()]
  }, [byStage])

  return {
    isRunning,
    stage,
    message,
    progress,
    pois,
    legs,
    log,
    elapsedS,
    // Only after a run has actually finished: before that, empty means "not yet".
    foundNothing: finished && pois.length === 0,
    error,
    start,
    cancel,
  }
}
