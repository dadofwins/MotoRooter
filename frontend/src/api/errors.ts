/**
 * The error model the API client throws.
 *
 * A component needs to know *why* a call failed without reading a sentence intended for a
 * human, so three distinctions are made explicit:
 *
 * - **`code`, not `detail`.** `ErrorResponse.code` is the stable machine-readable
 *   identifier; `detail` is prose the backend is free to reword. Switch on `code`.
 * - **Stubbed, not broken.** Replan, GPX export and Places enrichment answer 501 today.
 *   Those get their own class so the UI can say "coming soon" rather than "something went
 *   wrong", and so they stop being special the day the backend fills them in.
 * - **Unreachable, not refused.** A rejected `fetch` never produced a status. It is a
 *   different problem from a server that answered, and a different message to the rider.
 *
 * Aborts are deliberately *not* modelled here — see `isAbortError`.
 */
import type { ApiErrorCode } from './types'

/**
 * Codes that only ever originate here, because no server response can carry them: a body
 * that did not come from the app, and a request that never arrived at one.
 *
 * `not_implemented` deliberately is *not* in this list. The backend declares it in
 * `ErrorCode`, so it arrives generated — restating it here would recreate the
 * hand-maintained duplicate that generating the union was meant to remove.
 */
export type ClientErrorCode = 'unknown_error' | 'network_error'

/**
 * Any error code that can reach a caller.
 *
 * The union of known codes keeps `switch` autocompletion useful, and the open `string`
 * arm means a code the backend adds later still type-checks instead of breaking the
 * build. Handle the ones you care about and treat the rest as a generic failure.
 */
export type ErrorCode = ApiErrorCode | ClientErrorCode | (string & {})

interface ApiErrorInit {
  readonly status: number
  readonly code: ErrorCode
  readonly detail: string
  /** Raw parsed response body. For logging and debugging only — never for control flow. */
  readonly body?: unknown
  readonly cause?: unknown
}

/** A request that reached the server and came back as a failure. */
export class ApiError extends Error {
  /** HTTP status, or `0` when the request never reached the server. */
  readonly status: number
  readonly code: ErrorCode
  /** Human-readable message from the backend. Safe to show; never to branch on. */
  readonly detail: string
  readonly body: unknown

  constructor({ status, code, detail, body, cause }: ApiErrorInit) {
    super(`[${status}] ${code}: ${detail}`, cause === undefined ? undefined : { cause })
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
    this.body = body
  }
}

/**
 * An endpoint whose schema is frozen but whose implementation has not landed yet.
 *
 * The UI should present these as unfinished features, not as errors. Nothing about a call
 * site changes when the backend starts answering for real: the 501 simply stops arriving.
 */
export class ApiNotImplementedError extends ApiError {
  constructor({ detail, body }: { readonly detail: string; readonly body?: unknown }) {
    super({ status: 501, code: 'not_implemented', detail, body })
    this.name = 'ApiNotImplementedError'
  }
}

/** The request never got an answer: offline, DNS failure, connection refused. */
export class ApiNetworkError extends ApiError {
  constructor({ detail, cause }: { readonly detail: string; readonly cause?: unknown }) {
    super({ status: 0, code: 'network_error', detail, ...(cause === undefined ? {} : { cause }) })
    this.name = 'ApiNetworkError'
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

export function isNotImplemented(error: unknown): error is ApiNotImplementedError {
  return error instanceof ApiNotImplementedError
}

/**
 * Whether a rejection is an abort rather than a failure.
 *
 * The client rethrows aborts unchanged instead of wrapping them in an `ApiError`, because
 * an aborted request is not an error — it is a request the caller no longer wants.
 * `DragScheduler` supersedes its own in-flight requests constantly and identifies them
 * with exactly this predicate; wrapping them would turn every drag into an error report.
 * It keeps a private copy so the generic scheduler stays independent of this module.
 */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}
