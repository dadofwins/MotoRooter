import { describe, expect, it } from 'vitest'
import {
  ApiError,
  ApiNetworkError,
  ApiNotImplementedError,
  isApiError,
  isNotImplemented,
} from './errors'

/**
 * The error model exists so a component can branch on *why* a call failed without
 * string-matching a human-readable message. Three distinctions carry real UI weight:
 * a stable `code` to switch on, "not built yet" versus "broke", and "never reached the
 * server" versus "the server said no".
 */
describe('ApiError', () => {
  it('carries the stable code, the status, and the human detail separately', () => {
    const error = new ApiError({
      status: 404,
      code: 'trip_not_found',
      detail: "no trip named 'nope'",
    })

    expect(error.status).toBe(404)
    expect(error.code).toBe('trip_not_found')
    expect(error.detail).toBe("no trip named 'nope'")
  })

  it('is a real Error with a named class, so stacks and logs are readable', () => {
    const error = new ApiError({ status: 500, code: 'unknown_error', detail: 'boom' })

    expect(error).toBeInstanceOf(Error)
    expect(error.name).toBe('ApiError')
    // The message is for humans reading a log; UI code switches on `code`.
    expect(error.message).toContain('500')
    expect(error.message).toContain('unknown_error')
    expect(error.message).toContain('boom')
  })

  it('keeps the raw body for debugging when the server sent a non-contract shape', () => {
    const raw = { detail: [{ loc: ['body', 'waypoints'], msg: 'too short' }] }
    const error = new ApiError({
      status: 422,
      code: 'validation_error',
      detail: 'Request validation failed',
      body: raw,
    })

    expect(error.body).toEqual(raw)
  })
})

describe('ApiNotImplementedError', () => {
  it('is an ApiError, so a generic handler still catches it', () => {
    const error = new ApiNotImplementedError({ detail: 'GPX export is not implemented yet' })

    expect(error).toBeInstanceOf(ApiError)
    expect(error.name).toBe('ApiNotImplementedError')
  })

  it('pins status 501 and the not_implemented code, so the UI can say "coming soon"', () => {
    const error = new ApiNotImplementedError({ detail: 'replan is not implemented yet' })

    expect(error.status).toBe(501)
    expect(error.code).toBe('not_implemented')
  })
})

describe('ApiNetworkError', () => {
  it('reports status 0 — the request never reached the server', () => {
    const error = new ApiNetworkError({ detail: 'Failed to fetch' })

    expect(error).toBeInstanceOf(ApiError)
    expect(error.name).toBe('ApiNetworkError')
    expect(error.status).toBe(0)
    expect(error.code).toBe('network_error')
  })

  it('keeps the underlying cause, which is where the real diagnosis lives', () => {
    const cause = new TypeError('fetch failed')
    const error = new ApiNetworkError({ detail: 'fetch failed', cause })

    expect(error.cause).toBe(cause)
  })
})

describe('type guards', () => {
  it('isApiError accepts every client error and rejects unrelated throwables', () => {
    expect(isApiError(new ApiError({ status: 400, code: 'invalid_slug', detail: 'x' }))).toBe(true)
    expect(isApiError(new ApiNotImplementedError({ detail: 'x' }))).toBe(true)
    expect(isApiError(new ApiNetworkError({ detail: 'x' }))).toBe(true)

    expect(isApiError(new Error('plain'))).toBe(false)
    expect(isApiError({ status: 404, code: 'trip_not_found' })).toBe(false)
    expect(isApiError(null)).toBe(false)
  })

  it('isNotImplemented separates "coming soon" from a genuine failure', () => {
    expect(isNotImplemented(new ApiNotImplementedError({ detail: 'x' }))).toBe(true)
    expect(isNotImplemented(new ApiError({ status: 503, code: 'trip_storage_unavailable', detail: 'x' }))).toBe(
      false,
    )
    expect(isNotImplemented(new Error('plain'))).toBe(false)
  })
})
