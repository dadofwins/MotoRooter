import { describe, expect, it } from 'vitest'
import { replanErrorMessage, routeErrorMessage } from './routeErrorMessage'
import { ApiError, ApiNetworkError, ApiNotImplementedError } from '../api/errors'
import type { ApiErrorCode } from '../api/types'

/**
 * What a rider is told when routing fails.
 *
 * The error model already distinguishes an unreachable server from a refused request from
 * an unbuilt feature; the point of this is that the *UI* uses that distinction instead of
 * printing whatever string came back. `[400] invalid_request: [fake] 51 waypoints exceeds
 * provider maximum 50` is a sentence for a log, not for someone in a car park deciding
 * whether to try again.
 */
describe('routeErrorMessage', () => {
  it('tells a rider what to do about a route that does not exist', () => {
    const message = routeErrorMessage(
      new ApiError({ status: 422, code: 'no_route_found', detail: 'no route between points' }),
    )

    expect(message).toMatch(/no route/i)
    expect(message).toMatch(/mov/i) // suggests moving a point
  })

  it('separates "you are offline" from "the server said no"', () => {
    const offline = routeErrorMessage(new ApiNetworkError({ detail: 'Failed to fetch' }))
    const refused = routeErrorMessage(
      new ApiError({ status: 503, code: 'provider_unavailable', detail: 'upstream down' }),
    )

    expect(offline).toMatch(/connection|offline|reach/i)
    expect(offline).not.toBe(refused)
  })

  it('says when the day’s routing budget is gone, which is a real limit here', () => {
    // ORS's free tier is ~2,000-2,500 requests/day and this app can reach it.
    const message = routeErrorMessage(
      new ApiError({ status: 429, code: 'quota_exceeded', detail: 'daily quota spent' }),
    )

    expect(message).toMatch(/quota|limit|today/i)
  })

  it('presents an unbuilt feature as unbuilt rather than as a fault', () => {
    expect(routeErrorMessage(new ApiNotImplementedError({ detail: 'not implemented yet' }))).toMatch(
      /not (yet )?(built|available)|coming/i,
    )
  })

  it('never leaks an internal message, however unfamiliar the failure', () => {
    // The failure mode that prompted this: a bracketed status, a provider name and an
    // internal limit, rendered verbatim in the chat rail.
    const leaky = new ApiError({
      status: 400,
      code: 'invalid_request',
      detail: '[fake] 51 waypoints exceeds provider maximum 50',
    })

    const message = routeErrorMessage(leaky)

    expect(message).not.toContain('fake')
    expect(message).not.toContain('[400]')
    expect(message).not.toContain('provider maximum')
    expect(message.length).toBeGreaterThan(0)
  })

  it('handles a plain Error, which is what a bug in our own code looks like', () => {
    const message = routeErrorMessage(new TypeError('x.map is not a function'))

    expect(message).not.toContain('is not a function')
    expect(message.length).toBeGreaterThan(0)
  })
})

describe('replanErrorMessage', () => {
  /**
   * Discovery answers 501 when the instance has no search, model or Places credentials —
   * which is exactly how the offline backend runs. "Not built yet" would be wrong: it is
   * built, and this instance cannot reach what it needs.
   */
  it('explains a 501 as missing configuration, not as an unfinished feature', () => {
    const message = replanErrorMessage(
      new ApiNotImplementedError({
        detail: 'discovery (no search, model or Places credentials configured) is not implemented yet',
      }),
    )

    expect(message).toMatch(/credential|configur/i)
    expect(message).not.toMatch(/not built/i)
  })

  it('falls back to the shared mapping for everything else', () => {
    expect(replanErrorMessage(new ApiNetworkError({ detail: 'Failed to fetch' }))).toMatch(
      /connection|reach/i,
    )
    expect(
      replanErrorMessage(
        new ApiError({ status: 429, code: 'quota_exceeded', detail: 'daily quota spent' }),
      ),
    ).toMatch(/limit|today/i)
  })
})

/**
 * Codes that say something a rider can act on.
 *
 * Found while building place search: `rate_limited` had no message and fell through to
 * "something went wrong", which is actively misleading — a rider reads it, retries immediately,
 * and fails again. The backend went to the trouble of separating "too fast" from "budget spent"
 * and the UI was collapsing them.
 *
 * Not every code earns a sentence. `internal_error`, `validation_error` and the rest are bugs or
 * internals where the generic message is the honest one: naming them would tell a rider something
 * true and useless. These are the ones where knowing which failure it was changes what to do next.
 */
describe('codes a rider can act on', () => {
  const distinct: readonly [ApiErrorCode, RegExp][] = [
    // Wait and retry. Distinct from quota, which will not come back today.
    ['rate_limited', /moment|too many/i],
    // The assistant's budget, not routing's — the map still works.
    ['llm_quota_exceeded', /assistant/i],
    // The assistant is down; the mouse is not.
    ['llm_unavailable', /assistant/i],
    ['llm_refused', /assistant/i],
    // A route came back with gaps, which is about the route rather than the request.
    ['route_incomplete', /route/i],
    // A mode this engine cannot serve, which the picker can act on.
    ['unsupported_intent', /mode|intent/i],
  ]

  it.each(distinct)('says something specific for %s', (code, expected) => {
    const message = routeErrorMessage(new ApiError({ status: 400, code, detail: 'x' }))

    expect(message).toMatch(expected)
    expect(message).not.toMatch(/something went wrong/i)
  })

  it('tells "too fast" apart from "budget spent"', () => {
    // The distinction the backend built rate limiting to make. One waits, the other does not.
    const fast = routeErrorMessage(new ApiError({ status: 429, code: 'rate_limited', detail: 'x' }))
    const spent = routeErrorMessage(
      new ApiError({ status: 429, code: 'quota_exceeded', detail: 'x' }),
    )

    expect(fast).not.toBe(spent)
    expect(spent).toMatch(/tomorrow/i)
  })

  it('still says something honest for a code that is a bug rather than a condition', () => {
    // A rider can do nothing about an internal error, so naming it would be true and useless.
    expect(routeErrorMessage(new ApiError({ status: 500, code: 'internal_error', detail: 'x' }))).toMatch(
      /something went wrong/i,
    )
  })
})
