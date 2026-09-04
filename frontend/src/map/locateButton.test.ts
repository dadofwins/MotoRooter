/**
 * The explicit "Show where I am" press, as distinct from opening the map on the rider.
 *
 * Tim reported the button doing nothing. Three separate gates caused it, and each is correct
 * for the question it was written for — "should the map *open* on the rider?" — and wrong for
 * "the rider just pressed a button". An automatic camera placement must defer to a loaded
 * trip. An explicit action must always be honoured, and must be repeatable.
 *
 * These were written before the fix and each was watched to fail.
 */
import { renderHook, act, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useBrowserLocation, type BrowserLocator } from './browserLocation'

const HERE = { lat: 47.5962, lon: -120.6615 }

/**
 * A locator built once and held for the life of the hook, never constructed in the render
 * callback.
 *
 * The hook's own docstring warns about precisely this and the first draft of these tests
 * walked into it anyway: a new locator each render re-runs the permission effect, which calls
 * `setCanLocate(true)` again and undoes a refusal that had just taken it away — so the test
 * fails against correct code. Worth the helper rather than the discipline.
 */
function held(overrides: Partial<BrowserLocator> = {}): () => BrowserLocation {
  const stable: BrowserLocator = {
    permission: () => Promise.resolve<PermissionState>('prompt'),
    current: () => Promise.resolve(HERE),
    ...overrides,
  }
  return () => useBrowserLocation(stable)
}

type BrowserLocation = ReturnType<typeof useBrowserLocation>

describe('pressing the locate button more than once', () => {
  it('asks again on a second press', async () => {
    // The guard was "never a second after an answer", which is right for a prompt-once policy
    // and wrong for a button: a rider presses it again after moving, or after panning away.
    const current = vi.fn(() => Promise.resolve(HERE))
    const { result } = renderHook(held({ current }))

    await waitFor(() => {
      expect(result.current.canLocate).toBe(true)
    })

    act(() => {
      result.current.locate()
    })
    await waitFor(() => {
      expect(result.current.isLocating).toBe(false)
    })

    act(() => {
      result.current.locate()
    })
    await waitFor(() => {
      expect(current).toHaveBeenCalledTimes(2)
    })
  })

  it('does not start a second request while one is still in flight', async () => {
    // The half of the old guard worth keeping. Two presses during one fix is one request.
    let settle: (at: typeof HERE) => void = () => undefined
    const current = vi.fn(
      () =>
        new Promise<typeof HERE>((resolve) => {
          settle = resolve
        }),
    )
    const { result } = renderHook(held({ current }))

    await waitFor(() => {
      expect(result.current.canLocate).toBe(true)
    })

    act(() => {
      result.current.locate()
    })
    act(() => {
      result.current.locate()
    })
    expect(current).toHaveBeenCalledTimes(1)

    await act(async () => {
      settle(HERE)
      await Promise.resolve()
    })
  })

  it('still stops offering itself after a refusal', async () => {
    // Unchanged behaviour, asserted because the repeat fix is the thing most likely to break
    // it: re-offering a control the rider has just declined puts them one click from a prompt
    // they said no to.
    const { result } = renderHook(held({ current: () => Promise.resolve(null) }))

    await waitFor(() => {
      expect(result.current.canLocate).toBe(true)
    })

    act(() => {
      result.current.locate()
    })
    await waitFor(() => {
      expect(result.current.canLocate).toBe(false)
    })
  })
})
