import { describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { browserLocator, useBrowserLocation, type BrowserLocator } from './browserLocation'

/**
 * Opening the map where the rider is standing.
 *
 * Tim: *"is it easy to zoom roughly to the browser location when loading up the page"*. The two
 * lines that read a position are the easy part; **when to ask is the whole design**. A permission
 * prompt on page load asks for something before showing anything worth granting it for, and it is
 * the kind of thing people refuse once and never revisit.
 *
 * So: read the permission state, which does not prompt, and act on what it says.
 *
 * **Measured against a real browser before any of this was built** (2026-08-26, headless Chrome,
 * permission granted over the DevTools protocol for the granted case):
 *
 * | origin and history | `permissions.query` | `getCurrentPosition` |
 * |---|---|---|
 * | localhost, never asked | `prompt` | prompts |
 * | localhost, already granted | `granted` | resolves silently |
 * | plain `http://` on a LAN address | **`denied`** | "Only secure origins are allowed" |
 *
 * The third row is why there is no `isSecureContext` branch here: an insecure origin already
 * reports `denied`, so the rule that covers "the rider said no" covers it too. Worth having
 * checked — `navigator.geolocation` is still *present* on an insecure origin, so a presence test
 * would have said everything was fine.
 */

function locator(overrides: Partial<BrowserLocator> = {}): BrowserLocator {
  return {
    permission: vi.fn(() => Promise.resolve<PermissionState | 'unsupported'>('prompt')),
    current: vi.fn(() => Promise.resolve({ lat: 47.61, lon: -122.33 })),
    ...overrides,
  }
}

describe('useBrowserLocation', () => {
  it('opens where the rider is when they have already said yes', async () => {
    // The case worth having: no prompt, no control, the map simply opens somewhere useful.
    const from = locator({ permission: () => Promise.resolve('granted') })

    const { result } = renderHook(() => useBrowserLocation(from))

    await waitFor(() => expect(result.current.coordinate).toEqual({ lat: 47.61, lon: -122.33 }))
  })

  it('does not ask when nobody has been asked yet', async () => {
    // The rule the whole design turns on. A prompt on load is a question asked before the app has
    // shown anything worth answering it for.
    const from = locator()

    const { result } = renderHook(() => useBrowserLocation(from))

    await waitFor(() => expect(result.current.canLocate).toBe(true))
    expect(from.current).not.toHaveBeenCalled()
    expect(result.current.coordinate).toBeNull()
  })

  it('asks when the rider asks, which is what the control is for', async () => {
    const from = locator()
    const { result } = renderHook(() => useBrowserLocation(from))
    await waitFor(() => expect(result.current.canLocate).toBe(true))

    result.current.locate()

    await waitFor(() => expect(result.current.coordinate).toEqual({ lat: 47.61, lon: -122.33 }))
  })

  it('offers nothing where the answer is already no', async () => {
    // A control that can only fail is a control that lies. This also covers a plain-http origin,
    // which reports denied rather than prompt — measured, not assumed.
    const from = locator({ permission: () => Promise.resolve('denied') })

    const { result } = renderHook(() => useBrowserLocation(from))

    await waitFor(() => expect(result.current.canLocate).toBe(false))
    expect(from.current).not.toHaveBeenCalled()
  })

  it('offers nothing where the browser has no such thing', async () => {
    const from = locator({ permission: () => Promise.resolve('unsupported') })

    const { result } = renderHook(() => useBrowserLocation(from))

    await waitFor(() => expect(result.current.canLocate).toBe(false))
  })

  it('takes a refusal as an answer rather than an error', async () => {
    // Denied is a normal outcome. No alarm, and nothing that would ask again.
    const from = locator({ current: () => Promise.resolve(null) })
    const { result } = renderHook(() => useBrowserLocation(from))
    await waitFor(() => expect(result.current.canLocate).toBe(true))

    result.current.locate()

    await waitFor(() => expect(result.current.isLocating).toBe(false))
    expect(result.current.coordinate).toBeNull()
    expect(result.current.canLocate).toBe(false)
  })

  it('asks once, however many times the control is pressed', async () => {
    const from = locator()
    const { result } = renderHook(() => useBrowserLocation(from))
    await waitFor(() => expect(result.current.canLocate).toBe(true))

    result.current.locate()
    result.current.locate()

    await waitFor(() => expect(result.current.coordinate).not.toBeNull())
    expect(from.current).toHaveBeenCalledTimes(1)
  })
})

describe('browserLocator', () => {
  it('says unsupported rather than throwing where there is no such API', async () => {
    vi.stubGlobal('navigator', {})
    try {
      await expect(browserLocator().permission()).resolves.toBe('unsupported')
      await expect(browserLocator().current()).resolves.toBeNull()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('reads the permission without asking for it', async () => {
    // `permissions.query` reports the state and does not prompt — checked against Chrome, since
    // the entire design rests on it.
    const query = vi.fn(() => Promise.resolve({ state: 'granted' }))
    vi.stubGlobal('navigator', { permissions: { query }, geolocation: {} })
    try {
      await expect(browserLocator().permission()).resolves.toBe('granted')
      expect(query).toHaveBeenCalledWith({ name: 'geolocation' })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('gives up rather than hanging on a fix that never arrives', async () => {
    // A slow fix must not leave the control spinning. The map opening in the wrong place is
    // better than the map never settling.
    const getCurrentPosition = vi.fn(
      (
        _ok: PositionCallback,
        fail: PositionErrorCallback | null | undefined,
        _options?: PositionOptions,
      ) => {
        fail?.({ code: 3, message: 'timeout' } as GeolocationPositionError)
      },
    )
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } })
    try {
      await expect(browserLocator().current()).resolves.toBeNull()
      expect(getCurrentPosition.mock.calls[0]?.[2]).toMatchObject({ timeout: expect.any(Number) })
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
