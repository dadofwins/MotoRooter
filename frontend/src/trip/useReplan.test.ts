import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { needsReplan, useReplan } from './useReplan'
import { ApiNetworkError } from '../api/errors'
import { poi as poiFixture, trip as tripFixture } from '../api/fixtures'
import type { RequestOptions } from '../api/client'
import type { ReplanEvent, ReplanRequest } from '../api/types'

/**
 * The slow path, streamed.
 *
 * Discovery takes tens of seconds, so the point of streaming it is that pins appear as they
 * resolve — a rider watching an empty map for thirty seconds concludes the app is broken. The
 * run must also never block the fast path: dragging during a replan has to keep working.
 *
 * The subtlety in the contract is that `pois` is cumulative *per stage*, not overall. A later
 * stage's list restarts, so accumulating naively either loses earlier stages or duplicates
 * within one.
 */

function event(overrides: Partial<ReplanEvent> = {}): ReplanEvent {
  return { stage: 'discovery', message: '', pois: [], legs: [], progress: null, ...overrides }
}

function fakeClient(events: readonly ReplanEvent[]) {
  const replan = vi.fn(
    // eslint-disable-next-line @typescript-eslint/require-await
    async function* (_slug: string, _request: ReplanRequest, _options?: RequestOptions) {
      for (const item of events) yield item
    },
  )
  return { replan }
}

describe('needsReplan', () => {
  /**
   * Mirrors `Trip.needs_replan` on the backend, which is `planned_at is None or edited_at >
   * planned_at`. Derived here because the field is not serialised on `Trip` — only on
   * `TripSummary` — and a stale-suggestion flag the rider cannot see is worse than none.
   */
  it('is true for a trip nobody has planned yet', () => {
    expect(needsReplan(tripFixture({ planned_at: null }))).toBe(true)
  })

  it('is true once the route was edited after the last plan', () => {
    expect(
      needsReplan(
        tripFixture({ planned_at: '2026-08-25T18:00:00Z', edited_at: '2026-08-25T18:05:00Z' }),
      ),
    ).toBe(true)
  })

  it('is false while the plan is newer than the route', () => {
    expect(
      needsReplan(
        tripFixture({ planned_at: '2026-08-25T18:05:00Z', edited_at: '2026-08-25T18:00:00Z' }),
      ),
    ).toBe(false)
  })

  it('has nothing to say about a trip that does not exist', () => {
    expect(needsReplan(null)).toBe(false)
  })
})

describe('useReplan', () => {
  it('is not running until asked, because this is the explicit path', () => {
    // Never fired by a route edit: the slow path is user-triggered by design.
    const client = fakeClient([])

    const { result } = renderHook(() => useReplan(client))

    expect(result.current.isRunning).toBe(false)
    expect(client.replan).not.toHaveBeenCalled()
  })

  it('shows pins as they resolve rather than at the end', async () => {
    const client = fakeClient([
      event({ stage: 'discovery', message: 'Looking', pois: [poiFixture({ id: 'a' })] }),
      event({
        stage: 'discovery',
        message: 'Looking',
        pois: [poiFixture({ id: 'a' }), poiFixture({ id: 'b' })],
      }),
      event({ stage: 'done', message: 'Done' }),
    ])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    await waitFor(() => expect(result.current.pois).toHaveLength(2))
    expect(result.current.isRunning).toBe(false)
  })

  it('keeps what an earlier stage found, since each list is cumulative only per stage', async () => {
    // Discovery finds three; enrichment then reports its own two. Replacing wholesale loses
    // the first three, and appending duplicates within a stage.
    const client = fakeClient([
      event({
        stage: 'discovery',
        pois: [poiFixture({ id: 'a' }), poiFixture({ id: 'b' }), poiFixture({ id: 'c' })],
      }),
      event({ stage: 'enrichment', pois: [poiFixture({ id: 'a' }), poiFixture({ id: 'd' })] }),
      event({ stage: 'done' }),
    ])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    await waitFor(() => expect(result.current.isRunning).toBe(false))
    expect(result.current.pois.map((poi) => poi.id).sort()).toEqual(['a', 'b', 'c', 'd'])
  })

  it('reports which stage it is in and how far along', async () => {
    // "Working…" for thirty seconds is indistinguishable from a hang.
    const client = fakeClient([
      event({ stage: 'discovery', message: 'Searching for camps', progress: 0.4 }),
      event({ stage: 'done', message: 'Finished', progress: 1 }),
    ])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    await waitFor(() => expect(result.current.isRunning).toBe(false))
    expect(result.current.message).toBe('Finished')
  })

  it('says it found nothing rather than looking like it is still working', async () => {
    // Discovery genuinely returns nothing sometimes, and today often.
    const client = fakeClient([event({ stage: 'discovery' }), event({ stage: 'done' })])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    await waitFor(() => expect(result.current.isRunning).toBe(false))
    expect(result.current.foundNothing).toBe(true)
  })

  it('does not claim it found nothing before it has looked', () => {
    const { result } = renderHook(() => useReplan(fakeClient([])))

    expect(result.current.foundNothing).toBe(false)
  })

  it('reports a failure without an internal string', async () => {
    const client = {
      replan: vi.fn(
        // eslint-disable-next-line require-yield, @typescript-eslint/require-await
        async function* (_slug: string, _request: ReplanRequest, _options?: RequestOptions) {
          throw new ApiNetworkError({ detail: 'Failed to fetch' })
        },
      ),
    }
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.isRunning).toBe(false)
  })

  it('refuses to start twice at once', async () => {
    const client = {
      replan: vi.fn(
        // eslint-disable-next-line require-yield
        async function* (_slug: string, _request: ReplanRequest, _options?: RequestOptions) {
          await new Promise(() => undefined)
        },
      ),
    }
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })
    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    expect(client.replan).toHaveBeenCalledTimes(1)
  })

  it('abandons the run when it is cancelled', async () => {
    const signals: AbortSignal[] = []
    const client = {
      replan: vi.fn(
        // eslint-disable-next-line require-yield
        async function* (_slug: string, _request: ReplanRequest, options?: RequestOptions) {
          if (options?.signal !== undefined) signals.push(options.signal)
          await new Promise(() => undefined)
        },
      ),
    }
    const { result } = renderHook(() => useReplan(client))
    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    act(() => {
      result.current.cancel()
    })

    expect(signals[0]?.aborted).toBe(true)
    expect(result.current.isRunning).toBe(false)
  })

  it('abandons the run when unmounted, so nothing lands on a dead tree', async () => {
    const signals: AbortSignal[] = []
    const client = {
      replan: vi.fn(
        // eslint-disable-next-line require-yield
        async function* (_slug: string, _request: ReplanRequest, options?: RequestOptions) {
          if (options?.signal !== undefined) signals.push(options.signal)
          await new Promise(() => undefined)
        },
      ),
    }
    const { result, unmount } = renderHook(() => useReplan(client))
    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    unmount()

    expect(signals[0]?.aborted).toBe(true)
  })

  it('preserves the pinned POIs the rider chose, by asking the server to', async () => {
    const client = fakeClient([event({ stage: 'done' })])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })

    // The default the contract documents, sent explicitly rather than relied upon: a replan
    // that silently discarded hand-placed POIs would be the worst kind of surprise.
    expect(client.replan.mock.calls[0]?.[1].preserve_pinned).toBe(true)
  })
})

describe('the progress log', () => {
  /**
   * A rider watching a multi-minute operation needs to see it accumulate. Tim's words were "I
   * have no idea what it's doing and it's already been like a few minutes", so the log answers
   * what, and the elapsed count answers how long.
   *
   * Events will arrive fast and out of order once discovery is parallelised, so nothing here
   * may assume ordered stages or monotonic progress.
   */
  it('accumulates what it has done, newest first', async () => {
    const client = fakeClient([
      event({ stage: 'discovery', message: 'Searching near Rainy Pass' }),
      event({ stage: 'discovery', message: 'Found 5 leads' }),
      event({ stage: 'done', message: 'Finished' }),
    ])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.isRunning).toBe(false))

    expect(result.current.log.map((entry) => entry.message)).toEqual([
      'Finished',
      'Found 5 leads',
      'Searching near Rainy Pass',
    ])
  })

  it('collapses a message repeated back to back, which parallel emission will do', async () => {
    const client = fakeClient([
      event({ message: 'Judging candidates' }),
      event({ message: 'Judging candidates' }),
      event({ message: 'Judging candidates' }),
      event({ stage: 'done', message: 'Finished' }),
    ])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.isRunning).toBe(false))

    expect(result.current.log).toHaveLength(2)
  })

  it('keeps the log bounded, because a few hundred events would scroll away', async () => {
    const many = Array.from({ length: 60 }, (_, index) =>
      event({ message: `Step ${String(index)}` }),
    )
    const client = fakeClient([...many, event({ stage: 'done', message: 'Finished' })])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.isRunning).toBe(false))

    expect(result.current.log.length).toBeLessThanOrEqual(12)
    // The newest is kept; the oldest is what falls off.
    expect(result.current.log[0]?.message).toBe('Finished')
  })

  it('never lets the meter go backwards, whatever order events arrive in', async () => {
    // Parallel emission means a 40% event can land after a 60% one. A meter that retreats
    // reads as broken, so the highest seen wins: "at least this far" is stable and true.
    const client = fakeClient([
      event({ progress: 0.6 }),
      event({ progress: 0.3 }),
      event({ stage: 'done', progress: 0.9 }),
    ])
    const { result } = renderHook(() => useReplan(client))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.isRunning).toBe(false))

    expect(result.current.progress).toBeCloseTo(0.9, 6)
  })

  it('starts a new run with a clean log, rather than continuing the last one', async () => {
    // Two runs emitting different steps, so "the old ones are gone" is distinguishable from
    // "the same events happened again".
    let call = 0
    const client = {
      // eslint-disable-next-line @typescript-eslint/require-await
      replan: vi.fn(async function* (
        _slug: string,
        _request: ReplanRequest,
        _options?: RequestOptions,
      ) {
        call += 1
        yield event({ message: call === 1 ? 'First run' : 'Second run' })
        yield event({ stage: 'done', message: 'Finished' })
      }),
    }
    const { result } = renderHook(() => useReplan(client))
    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.isRunning).toBe(false))

    await act(async () => {
      result.current.start('wabdr-north')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.isRunning).toBe(false))

    const messages = result.current.log.map((entry) => entry.message)
    expect(messages).toContain('Second run')
    expect(messages).not.toContain('First run')
  })

  it('measures elapsed time rather than counting ticks', async () => {
    // Browsers throttle `setInterval` hard in a background tab, and this is exactly the case
    // the counter exists for: Tim's complaint was about a wait long enough that he went and
    // did something else. Counting ticks made a five-minute wait display as barely one.
    vi.useFakeTimers()
    try {
      const client = {
        replan: vi.fn(
          // eslint-disable-next-line require-yield
          async function* (_slug: string, _request: ReplanRequest, _options?: RequestOptions) {
            await new Promise(() => undefined)
          },
        ),
      }
      const { result } = renderHook(() => useReplan(client))

      await act(async () => {
        result.current.start('wabdr-north')
        await Promise.resolve()
      })

      // One tick delivered, sixty seconds of wall clock passed — a throttled background tab.
      await act(async () => {
        vi.setSystemTime(Date.now() + 60_000)
        await vi.advanceTimersByTimeAsync(1000)
      })

      expect(result.current.elapsedS).toBeGreaterThanOrEqual(60)
    } finally {
      vi.useRealTimers()
    }
  })

  it('counts the seconds, because "a few minutes" is the complaint it answers', async () => {
    vi.useFakeTimers()
    try {
      const client = {
        replan: vi.fn(
          // eslint-disable-next-line require-yield
          async function* (_slug: string, _request: ReplanRequest, _options?: RequestOptions) {
            await new Promise(() => undefined)
          },
        ),
      }
      const { result } = renderHook(() => useReplan(client))

      await act(async () => {
        result.current.start('wabdr-north')
        await Promise.resolve()
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000)
      })

      expect(result.current.elapsedS).toBeGreaterThanOrEqual(5)
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops counting when the run stops', async () => {
    vi.useFakeTimers()
    try {
      const client = fakeClient([event({ stage: 'done' })])
      const { result } = renderHook(() => useReplan(client))
      await act(async () => {
        result.current.start('wabdr-north')
        await Promise.resolve()
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000)
      })
      const settled = result.current.elapsedS

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000)
      })

      expect(result.current.elapsedS).toBe(settled)
    } finally {
      vi.useRealTimers()
    }
  })
})
