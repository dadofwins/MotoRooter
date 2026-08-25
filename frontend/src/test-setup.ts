import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'

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
})
