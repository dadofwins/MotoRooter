import { describe, expect, it } from 'vitest'
import { routeErrorMessage } from './routeErrorMessage'
import { ApiError, ApiNetworkError, ApiNotImplementedError } from '../api/errors'

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
