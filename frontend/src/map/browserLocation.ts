/**
 * Opening the map where the rider is standing.
 *
 * Tim: *"is it easy to zoom roughly to the browser location when loading up the page"*. Reading a
 * position is two lines; **when to ask is the whole design**. A permission prompt on page load
 * asks for something before the app has shown anything worth granting it for, and it is the kind
 * of thing people refuse once and never revisit.
 *
 * So the state is read rather than the permission requested, and each answer means something
 * different:
 *
 * - **granted** — locate at once. No prompt, no control; the map just opens somewhere useful.
 * - **prompt** — do not ask. Offer a control, so the question arrives when somebody wants the
 *   thing it is for.
 * - **denied** — offer nothing. A control that can only fail is a control that lies.
 *
 * **Measured against a real browser before this was written** (headless Chrome, with the granted
 * case set up over the DevTools protocol):
 *
 * | origin and history | `permissions.query` | `getCurrentPosition` |
 * |---|---|---|
 * | localhost, never asked | `prompt` | prompts |
 * | localhost, already granted | `granted` | resolves silently |
 * | plain `http://` on a LAN address | **`denied`** | "Only secure origins are allowed" |
 *
 * That third row is why there is no `isSecureContext` branch: an insecure origin already reports
 * `denied`, so the rule for "the rider said no" covers it. Worth having checked, because
 * `navigator.geolocation` is still *present* there — a presence test would have said all was well
 * right up to the first deployment served over plain HTTP.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Coordinate } from '../api/types'

/**
 * How long to wait for a fix.
 *
 * A phone with a cold GPS can take tens of seconds. The map is already up and usable by then, so
 * this bounds the control's busy state rather than anything the rider is blocked on — and a
 * control that spins forever is worse than one that quietly gives up.
 */
const FIX_TIMEOUT_MS = 10_000

/** The browser's own answers, behind a seam so tests do not stub globals. */
export interface BrowserLocator {
  /** The permission state, without asking for it. `unsupported` where there is no such API. */
  readonly permission: () => Promise<PermissionState | 'unsupported'>
  /** A position, or null for any reason at all — refusal included, which is not an error. */
  readonly current: () => Promise<Coordinate | null>
}

export function browserLocator(): BrowserLocator {
  return {
    permission: async () => {
      const permissions = navigator.permissions as Permissions | undefined
      if (permissions === undefined || navigator.geolocation === undefined) return 'unsupported'
      try {
        return (await permissions.query({ name: 'geolocation' })).state
      } catch {
        // Some browsers reject the query for a name they do not know. Not knowing is not denial,
        // so this falls back to the state that offers the control and asks nothing.
        return 'prompt'
      }
    },
    current: async () =>
      new Promise<Coordinate | null>((resolve) => {
        if (navigator.geolocation === undefined) {
          resolve(null)
          return
        }
        navigator.geolocation.getCurrentPosition(
          (position) => {
            resolve({ lat: position.coords.latitude, lon: position.coords.longitude })
          },
          // Refused, unavailable, timed out. All the same answer to the only question here.
          () => {
            resolve(null)
          },
          { timeout: FIX_TIMEOUT_MS, maximumAge: 5 * 60_000 },
        )
      }),
  }
}

export interface BrowserLocation {
  /** Where the rider is, once that is known and only if it ever becomes known. */
  readonly coordinate: Coordinate | null
  /** Whether asking could still work — false once refused, denied, or unsupported. */
  readonly canLocate: boolean
  readonly isLocating: boolean
  readonly locate: () => void
}

export function useBrowserLocation(given?: BrowserLocator): BrowserLocation {
  /**
   * The default, built once for the life of the component rather than per render.
   *
   * A default *argument* constructs a new object on every render, and the effect below depends
   * on it — this project has been bitten by exactly that, when a capabilities object rebuilt each
   * render destroyed a whole drag gesture. A module singleton would fix the churn and make the
   * thing untestable, since it would cache whichever browser it first saw.
   */
  const [fallback] = useState(browserLocator)
  const from = given ?? fallback

  const [coordinate, setCoordinate] = useState<Coordinate | null>(null)
  const [canLocate, setCanLocate] = useState(false)
  const [isLocating, setLocating] = useState(false)
  /**
   * One request at a time.
   *
   * It used to mean "and never a second, ever", which was right while this only opened the map
   * on load and wrong the moment it also backed a button. A rider presses "show where I am"
   * again after riding somewhere, or after panning off themselves, and the second press did
   * nothing at all — the control stayed on screen and stopped working after one use.
   *
   * Concurrency is the part worth keeping: two presses during one fix should be one request.
   * What ends the offer permanently is a refusal, below, which is a different rule and still
   * holds.
   */
  const inFlight = useRef(false)

  const ask = useCallback(() => {
    if (inFlight.current) return
    inFlight.current = true
    setLocating(true)
    void from.current().then((at) => {
      inFlight.current = false
      setLocating(false)
      setCoordinate(at)
      // A refusal ends it. Re-offering the control would put the rider one click from a prompt
      // they have just declined.
      if (at === null) setCanLocate(false)
    })
  }, [from])

  useEffect(() => {
    let live = true
    void from.permission().then((state) => {
      if (!live) return
      if (state === 'granted') {
        ask()
        return
      }
      setCanLocate(state === 'prompt')
    })
    return () => {
      live = false
    }
  }, [from, ask])

  return { coordinate, canLocate, isLocating, locate: ask }
}
