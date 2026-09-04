import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'

/**
 * A `localStorage` for tests, because this jsdom does not provide one.
 *
 * Verified rather than assumed: `typeof localStorage` is `undefined` in this environment even
 * though the page has a real origin. Production code treats storage as something that may
 * refuse — Safari in private browsing throws on write — so the absence is survivable there,
 * but a test that wants to assert a preference persisted needs somewhere for it to persist.
 */
if (typeof globalThis.localStorage === 'undefined') {
  const entries = new Map<string, string>()
  const memory: Storage = {
    get length() {
      return entries.size
    },
    clear: () => entries.clear(),
    getItem: (key) => entries.get(key) ?? null,
    key: (index) => [...entries.keys()][index] ?? null,
    removeItem: (key) => {
      entries.delete(key)
    },
    setItem: (key, value) => {
      entries.set(key, value)
    },
  }
  Object.defineProperty(globalThis, 'localStorage', { value: memory, configurable: true })
}

/**
 * Unmount and clear the document between tests.
 *
 * React Testing Library registers its own cleanup when it is first imported, which is
 * enough only while every test file runs in its own process. Share a module registry — as
 * `--poolOptions.forks.singleFork` does, and as any future change to the pool would — and
 * the registration happens once, in whichever file imported RTL first. Every later file
 * then runs with no cleanup at all, accumulating mounted components until a `getBy*` query
 * finds two of something and fails in a file that has nothing to do with the cause.
 *
 * That is not a hypothetical: it made the suite pass in parallel and fail single-threaded,
 * which is worse than failing outright, because `make check` is the gate everything else
 * relies on.
 *
 * Clearing the body as well as unmounting covers nodes a test attached directly — pins
 * handed to a fake marker, probe containers — which RTL does not know about and so leaves
 * behind.
 */
afterEach(() => {
  cleanup()
  document.body.replaceChildren()
  // Storage is shared state exactly as the document is. A unit preference written by one
  // test made a later one see kilometres where it expected the default — the same
  // cross-test pollution as leftover DOM, through a different door.
  try {
    localStorage.clear()
  } catch {
    // Nothing to clear if storage is unavailable.
  }
  // The URL is shared state too — the third door after the document and storage. A test that
  // put ?trip=... there made a later one load that trip instead of creating one, so it failed
  // only in shuffled order. Anything global is reset here rather than per file.
  window.history.replaceState(null, '', '/')
})
