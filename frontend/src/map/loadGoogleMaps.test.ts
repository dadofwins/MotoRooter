import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createGoogleMapsLoader } from './loadGoogleMaps'

/**
 * Loading the Maps JS API is the one place the app can fail before it draws anything, and
 * every failure mode here looks identical to the user — a blank grey rectangle. So each
 * one has to produce a distinguishable error instead: no key configured, script blocked or
 * offline, and the referrer-restriction rejection that will happen the first time this runs
 * from an origin the key does not allow.
 */

/** The one script tag the loader is allowed to inject. */
function injectedScript(): HTMLScriptElement | null {
  return document.querySelector<HTMLScriptElement>('script[data-motorooter-maps]')
}

/** Stands in for the real API arriving on `window`. */
function simulateApiReady(): void {
  const maps = { Map: vi.fn(), Polyline: vi.fn() }
  Object.defineProperty(window, 'google', { value: { maps }, configurable: true, writable: true })
  const script = injectedScript()
  if (script === null) throw new Error('no script was injected')
  const callbackName = new URL(script.src).searchParams.get('callback')
  if (callbackName === null) throw new Error('script has no callback parameter')
  const callback = (window as unknown as Record<string, unknown>)[callbackName]
  if (typeof callback !== 'function') throw new Error(`callback ${callbackName} is not on window`)
  ;(callback as () => void)()
}

beforeEach(() => {
  document.head.replaceChildren()
  document.body.replaceChildren()
  Reflect.deleteProperty(window, 'google')
})

describe('createGoogleMapsLoader', () => {
  it('injects one script carrying the key, the async loading hint and the libraries', async () => {
    const load = createGoogleMapsLoader({ apiKey: 'browser-key-123', libraries: ['maps', 'marker'] })

    const pending = load()
    const script = injectedScript()
    expect(script).not.toBeNull()

    const url = new URL(script?.src ?? '')
    expect(url.origin + url.pathname).toBe('https://maps.googleapis.com/maps/api/js')
    expect(url.searchParams.get('key')).toBe('browser-key-123')
    expect(url.searchParams.get('libraries')).toBe('maps,marker')
    // Without loading=async the API logs a performance warning and blocks parsing.
    expect(url.searchParams.get('loading')).toBe('async')
    expect(script?.async).toBe(true)

    simulateApiReady()
    await expect(pending).resolves.toBeDefined()
  })

  it('resolves with the maps namespace, not with the window', async () => {
    const load = createGoogleMapsLoader({ apiKey: 'k' })

    const pending = load()
    simulateApiReady()

    const maps = await pending
    expect(maps).toBe(window.google.maps)
  })

  it('injects one script however many times it is called', async () => {
    const load = createGoogleMapsLoader({ apiKey: 'k' })

    const first = load()
    const second = load()
    expect(document.querySelectorAll('script[data-motorooter-maps]')).toHaveLength(1)

    simulateApiReady()
    expect(await first).toBe(await second)

    // A later call, after resolution, still must not add a second script.
    await load()
    expect(document.querySelectorAll('script[data-motorooter-maps]')).toHaveLength(1)
  })

  it('uses an API already on the page instead of loading it again', async () => {
    const maps = { Map: vi.fn() }
    Object.defineProperty(window, 'google', { value: { maps }, configurable: true, writable: true })
    const load = createGoogleMapsLoader({ apiKey: 'k' })

    await expect(load()).resolves.toBe(maps)
    expect(injectedScript()).toBeNull()
  })

  it('fails loudly when no key is configured, without injecting anything', async () => {
    // The common local-dev mistake: .env.local missing. A blank map with a console warning
    // is not a diagnosis, so this has to be an error the UI can render.
    const load = createGoogleMapsLoader({ apiKey: '' })

    await expect(load()).rejects.toThrow(/VITE_GOOGLE_MAPS_BROWSER_KEY/)
    expect(injectedScript()).toBeNull()
  })

  it('rejects when the script cannot load at all', async () => {
    const load = createGoogleMapsLoader({ apiKey: 'k' })

    const pending = load()
    injectedScript()?.dispatchEvent(new Event('error'))

    await expect(pending).rejects.toThrow(/could not be loaded/i)
  })

  it('retries after a failure rather than caching the rejection forever', async () => {
    // A referrer-restricted key rejected once should not poison the loader for the rest of
    // the session — the user may be on a flaky connection in a parking lot.
    const load = createGoogleMapsLoader({ apiKey: 'k' })

    const failed = load()
    injectedScript()?.dispatchEvent(new Event('error'))
    await expect(failed).rejects.toThrow()

    const retried = load()
    expect(injectedScript()).not.toBeNull()
    simulateApiReady()
    await expect(retried).resolves.toBeDefined()
  })

  it('leaves no callback behind on window', async () => {
    const load = createGoogleMapsLoader({ apiKey: 'k' })

    const pending = load()
    const callbackName = new URL(injectedScript()?.src ?? '').searchParams.get('callback') ?? ''
    expect(callbackName).not.toBe('')
    simulateApiReady()
    await pending

    expect(callbackName in window).toBe(false)
  })
})
