/**
 * Turns a routing failure into something worth showing a rider.
 *
 * `ErrorResponse.detail` is written for whoever reads the logs — `[fake] 51 waypoints
 * exceeds provider maximum 50` — and putting it on screen tells the user nothing they can
 * act on while leaking the shape of the system. The stable `code` is what carries meaning,
 * which is the whole reason the contract has one.
 *
 * The distinctions worth making are the ones that change what the rider does next: try
 * again now, try again later, move a point, or wait for a feature to exist.
 */
import { isApiError, isNotImplemented, type ErrorCode } from '../api/errors'
import { ApiNetworkError } from '../api/errors'

const BY_CODE: Partial<Record<ErrorCode, string>> = {
  no_route_found: 'No route found between those points. Try moving one of them.',
  quota_exceeded: 'Routing has hit its limit for today. It will work again tomorrow.',
  provider_unavailable: 'The routing service is not responding. Try again in a moment.',
  trip_storage_unavailable: 'Storage is not responding. Try again in a moment.',
  invalid_slug: 'That trip name cannot be used. Try letters, numbers and hyphens.',
  trip_not_found: 'That trip no longer exists.',
  trip_already_exists: 'A trip with that name already exists. Choose another.',
  trip_modified_concurrently: 'Someone else edited this trip first. Reload to see their version.',
}

const UNREACHABLE = 'Could not reach the server. Check your connection and try again.'
const UNBUILT = 'That is not built yet.'
const UNKNOWN = 'Something went wrong while routing. Try again.'
const PLACE_UNKNOWN = 'Details for this place could not be loaded.'

/** The same mapping, for a place lookup rather than a route. */
export function placeErrorMessage(error: unknown): string {
  if (error instanceof ApiNetworkError) return UNREACHABLE
  if (isNotImplemented(error)) return UNBUILT
  if (isApiError(error)) return BY_CODE[error.code] ?? PLACE_UNKNOWN
  return PLACE_UNKNOWN
}

export function routeErrorMessage(error: unknown): string {
  // A request that never arrived is a different problem from one that was refused, and the
  // rider can actually do something about this one.
  if (error instanceof ApiNetworkError) return UNREACHABLE
  if (isNotImplemented(error)) return UNBUILT
  if (isApiError(error)) return BY_CODE[error.code] ?? UNKNOWN

  // A plain Error here is a bug in our own code. The message belongs in the console, not on
  // screen, but the rider still needs to be told the route did not happen.
  return UNKNOWN
}
