/**
 * Loads the Google Maps JavaScript API.
 *
 * Hand-rolled rather than pulled from `@googlemaps/js-api-loader`, for two reasons: the
 * loader is thirty lines, and every failure here otherwise renders as the same blank grey
 * rectangle. Distinguishing "no key configured" from "script blocked" from "this origin is
 * not on the key's referrer allow-list" is the whole job, and it needs to be testable.
 *
 * A factory rather than a module-level singleton so the cache belongs to an instance:
 * the app makes one, each test makes its own, and no reset hook has to exist in
 * production code.
 */

/** The subset of the API surface the app uses. Provided by `@types/google.maps`. */
export type GoogleMaps = typeof google.maps

export interface GoogleMapsLoaderOptions {
  /**
   * The browser key. Necessarily public — it ships in the bundle — so it must be
   * restricted by HTTP referrer and by API, and must never be a server-side key.
   */
  readonly apiKey: string
  /** Libraries to request up front, e.g. `marker` for advanced markers. */
  readonly libraries?: readonly string[]
  /** Release channel. `weekly` keeps `colorScheme` and vector rendering available. */
  readonly version?: string
  /**
   * How long to wait before giving up.
   *
   * A captive portal accepts the connection and then holds it open, so the script neither
   * loads nor errors. Without a deadline the pane reads "Loading map…" indefinitely and the
   * retry that would fix it never appears.
   */
  readonly timeoutMs?: number
}

const DEFAULT_TIMEOUT_MS = 15_000

/** How often an adopting loader checks whether the shared script has produced the API. */
const ADOPT_POLL_MS = 50

/** Marks the one bootstrap script, and is how a second loader finds it. */
const SCRIPT_MARKER = 'data-motorooter-maps'

export type GoogleMapsLoader = () => Promise<GoogleMaps>

const BOOTSTRAP_URL = 'https://maps.googleapis.com/maps/api/js'

/** Maps' bootstrap only accepts a global callback name, so one has to exist briefly. */
let callbackSequence = 0

function alreadyLoaded(): GoogleMaps | null {
  const google = (window as { google?: { maps?: GoogleMaps } }).google
  return google?.maps ?? null
}

export function createGoogleMapsLoader(options: GoogleMapsLoaderOptions): GoogleMapsLoader {
  let pending: Promise<GoogleMaps> | null = null

  return function load(): Promise<GoogleMaps> {
    const loaded = alreadyLoaded()
    if (loaded !== null) return Promise.resolve(loaded)
    if (pending !== null) return pending

    if (options.apiKey === '') {
      // Not cached: configuring the key and reloading should just work.
      return Promise.reject(
        new Error(
          'No Google Maps browser key. Set VITE_GOOGLE_MAPS_BROWSER_KEY in frontend/.env.local ' +
            'to a key restricted to the Maps JavaScript API and to this origin.',
        ),
      )
    }

    pending = new Promise<GoogleMaps>((resolve, reject) => {
      const callbackName = `__motorooterMapsReady${String(++callbackSequence)}`
      // Google's bootstrap misbehaves if it is included twice, so a script another loader
      // instance already injected is adopted rather than duplicated.
      const existing = document.querySelector<HTMLScriptElement>(`script[${SCRIPT_MARKER}]`)
      const script = existing ?? document.createElement('script')

      let timer: ReturnType<typeof setTimeout> | null = null
      const cleanUp = (): void => {
        Reflect.deleteProperty(window, callbackName)
        if (timer !== null) clearTimeout(timer)
        timer = null
      }

      timer = setTimeout(() => {
        cleanUp()
        pending = null // retryable, same as an outright failure
        reject(
          new Error(
            'The Google Maps script took too long to load. The connection may be blocked by a ' +
              'captive portal or a content blocker.',
          ),
        )
      }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS)

      if (existing !== null) {
        // Someone else already asked for it. Only the first loader's callback name is in
        // the script URL, so this one cannot be called back — it watches for the API
        // appearing on `window` instead, which is the signal that does not depend on whose
        // callback the bootstrap holds. The shared deadline above still bounds the wait.
        const poll = setInterval(() => {
          const maps = alreadyLoaded()
          if (maps === null) return
          clearInterval(poll)
          cleanUp()
          resolve(maps)
        }, ADOPT_POLL_MS)

        existing.addEventListener('error', () => {
          clearInterval(poll)
          cleanUp()
          pending = null
          reject(new Error('The Google Maps script could not be loaded.'))
        })
        return
      }

      Object.defineProperty(window, callbackName, {
        value: () => {
          cleanUp()
          const maps = alreadyLoaded()
          if (maps === null) {
            reject(new Error('Google Maps reported ready but exposed no API on window.'))
            return
          }
          resolve(maps)
        },
        configurable: true,
        writable: true,
      })

      const url = new URL(BOOTSTRAP_URL)
      url.searchParams.set('key', options.apiKey)
      url.searchParams.set('v', options.version ?? 'weekly')
      if (options.libraries !== undefined && options.libraries.length > 0) {
        url.searchParams.set('libraries', options.libraries.join(','))
      }
      // Documented pairing: `loading=async` plus a callback, or the API logs a performance
      // warning and blocks the parser.
      url.searchParams.set('loading', 'async')
      url.searchParams.set('callback', callbackName)

      script.src = url.toString()
      script.async = true
      script.setAttribute(SCRIPT_MARKER, 'true')
      script.addEventListener('error', () => {
        cleanUp()
        script.remove()
        // Retryable: a dropped connection on the road should not disable the map for the
        // rest of the session.
        pending = null
        reject(
          new Error(
            'The Google Maps script could not be loaded. Check the connection, and check that ' +
              'this origin is allowed by the browser key’s referrer restrictions.',
          ),
        )
      })

      document.head.append(script)
    })

    return pending
  }
}
