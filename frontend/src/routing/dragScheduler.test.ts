import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DragScheduler } from './dragScheduler'

/**
 * The fast path. Three properties matter and each has burned real map editors:
 * live updates must be throttled per provider, the drag-end commit must always fire,
 * and out-of-order responses must never overwrite a newer result.
 */

interface Req {
  id: string
}
interface Res {
  id: string
}

/** A routing function whose responses resolve only when explicitly released. */
function deferredRouter() {
  const pending: { req: Req; resolve: (r: Res) => void; signal: AbortSignal }[] = []
  const route = (req: Req, signal: AbortSignal) =>
    new Promise<Res>((resolve) => pending.push({ req, resolve, signal }))
  return {
    route,
    pending,
    releaseAll: () => pending.splice(0).forEach((p) => p.resolve({ id: p.req.id })),
    release: (index: number) => {
      const [p] = pending.splice(index, 1)
      p?.resolve({ id: p.req.id })
    },
  }
}

function makeScheduler(intervalMs: number | null, router = deferredRouter()) {
  const onPreview = vi.fn<(r: Res) => void>()
  const onCommit = vi.fn<(r: Res) => void>()
  const scheduler = new DragScheduler<Req, Res>({
    intervalMs,
    route: router.route,
    onPreview,
    onCommit,
  })
  return { scheduler, router, onPreview, onCommit }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('throttling during a drag', () => {
  it('routes immediately on the first update', () => {
    const { scheduler, router } = makeScheduler(1000)
    scheduler.update({ id: 'a' })
    expect(router.pending).toHaveLength(1)
  })

  it('suppresses a second update inside the interval', () => {
    const { scheduler, router } = makeScheduler(1000)
    scheduler.update({ id: 'a' })
    router.releaseAll()
    vi.advanceTimersByTime(200)
    scheduler.update({ id: 'b' })
    expect(router.pending).toHaveLength(0)
  })

  it('issues the latest suppressed update once the interval elapses', async () => {
    const { scheduler, router } = makeScheduler(1000)
    scheduler.update({ id: 'a' })
    router.releaseAll()
    scheduler.update({ id: 'b' })
    scheduler.update({ id: 'c' })
    await vi.advanceTimersByTimeAsync(1000)
    expect(router.pending.map((p) => p.req.id)).toEqual(['c'])
  })

  it('keeps the line alive rather than waiting for a pause', () => {
    // Leading-edge throttle, not debounce: a continuous drag still produces updates.
    const { scheduler, router } = makeScheduler(1000)
    for (let i = 0; i < 5; i++) {
      scheduler.update({ id: `p${i}` })
      vi.advanceTimersByTime(300)
    }
    expect(router.pending.length).toBeGreaterThan(0)
  })

  it('reports previews, not commits, for mid-drag results', async () => {
    const { scheduler, router, onPreview, onCommit } = makeScheduler(1000)
    scheduler.update({ id: 'a' })
    router.releaseAll()
    await vi.advanceTimersByTimeAsync(0)
    expect(onPreview).toHaveBeenCalledWith({ id: 'a' })
    expect(onCommit).not.toHaveBeenCalled()
  })
})

describe('preview-only providers', () => {
  it('issues no request during the drag when the interval is null', () => {
    const { scheduler, router } = makeScheduler(null)
    scheduler.update({ id: 'a' })
    vi.advanceTimersByTime(10_000)
    expect(router.pending).toHaveLength(0)
  })

  it('still routes on release', () => {
    const { scheduler, router } = makeScheduler(null)
    scheduler.update({ id: 'a' })
    scheduler.end({ id: 'final' })
    expect(router.pending.map((p) => p.req.id)).toEqual(['final'])
  })
})

describe('drag end', () => {
  it('always routes on release, even immediately after a throttled update', () => {
    const { scheduler, router } = makeScheduler(1000)
    scheduler.update({ id: 'a' })
    router.releaseAll()
    scheduler.end({ id: 'final' })
    expect(router.pending.map((p) => p.req.id)).toEqual(['final'])
  })

  it('commits the release result', async () => {
    const { scheduler, router, onCommit } = makeScheduler(1000)
    scheduler.end({ id: 'final' })
    router.releaseAll()
    await vi.advanceTimersByTimeAsync(0)
    expect(onCommit).toHaveBeenCalledWith({ id: 'final' })
  })

  it('cancels a pending throttled update', async () => {
    const { scheduler, router } = makeScheduler(1000)
    scheduler.update({ id: 'a' })
    router.releaseAll()
    scheduler.update({ id: 'b' }) // queued behind the throttle
    scheduler.end({ id: 'final' })
    await vi.advanceTimersByTimeAsync(5000)
    expect(router.pending.map((p) => p.req.id)).toEqual(['final'])
  })

  it('starts a fresh throttle window for the next drag', () => {
    const { scheduler, router } = makeScheduler(1000)
    scheduler.update({ id: 'a' })
    router.releaseAll()
    scheduler.end({ id: 'final' })
    router.releaseAll()
    scheduler.update({ id: 'next' })
    expect(router.pending.map((p) => p.req.id)).toEqual(['next'])
  })
})

describe('out-of-order responses', () => {
  it('ignores a stale preview that resolves after a newer one', async () => {
    const { scheduler, router, onPreview } = makeScheduler(1000)
    scheduler.update({ id: 'first' })
    await vi.advanceTimersByTimeAsync(1000)
    scheduler.update({ id: 'second' })
    expect(router.pending).toHaveLength(2)

    router.release(1) // newer resolves first
    await vi.advanceTimersByTimeAsync(0)
    router.release(0) // older arrives late
    await vi.advanceTimersByTimeAsync(0)

    expect(onPreview).toHaveBeenCalledTimes(1)
    expect(onPreview).toHaveBeenCalledWith({ id: 'second' })
  })

  it('never lets a late preview overwrite a commit', async () => {
    // The bug this prevents: the user releases, then a slow mid-drag response
    // lands and silently reverts the committed route.
    const { scheduler, router, onPreview, onCommit } = makeScheduler(1000)
    scheduler.update({ id: 'stale' })
    scheduler.end({ id: 'final' })
    router.releaseAll()
    await vi.advanceTimersByTimeAsync(0)

    expect(onCommit).toHaveBeenCalledWith({ id: 'final' })
    expect(onPreview).not.toHaveBeenCalled()
  })

  it('aborts superseded requests', async () => {
    const { scheduler, router } = makeScheduler(1000)
    scheduler.update({ id: 'first' })
    const first = router.pending[0]!
    await vi.advanceTimersByTimeAsync(1000)
    scheduler.update({ id: 'second' })
    expect(first.signal.aborted).toBe(true)
  })
})

describe('cancel', () => {
  it('aborts in flight work and issues nothing further', async () => {
    const { scheduler, router, onPreview, onCommit } = makeScheduler(1000)
    scheduler.update({ id: 'a' })
    scheduler.cancel()
    expect(router.pending[0]!.signal.aborted).toBe(true)
    router.releaseAll()
    await vi.advanceTimersByTimeAsync(5000)
    expect(onPreview).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()
  })
})

describe('failures', () => {
  it('surfaces errors without breaking the next update', async () => {
    const onError = vi.fn()
    let attempt = 0
    const scheduler = new DragScheduler<Req, Res>({
      intervalMs: 1000,
      route: (req) => (attempt++ === 0 ? Promise.reject(new Error('boom')) : Promise.resolve({ id: req.id })),
      onPreview: vi.fn(),
      onCommit: vi.fn(),
      onError,
    })
    scheduler.update({ id: 'a' })
    await vi.advanceTimersByTimeAsync(0)
    expect(onError).toHaveBeenCalled()

    scheduler.end({ id: 'b' })
    await vi.advanceTimersByTimeAsync(0)
    expect(attempt).toBe(2)
  })

  it('ignores abort errors', async () => {
    const onError = vi.fn()
    const scheduler = new DragScheduler<Req, Res>({
      intervalMs: 1000,
      route: () => Promise.reject(new DOMException('aborted', 'AbortError')),
      onPreview: vi.fn(),
      onCommit: vi.fn(),
      onError,
    })
    scheduler.update({ id: 'a' })
    await vi.advanceTimersByTimeAsync(0)
    expect(onError).not.toHaveBeenCalled()
  })
})
