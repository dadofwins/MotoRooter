/**
 * One drag gesture over the route line.
 *
 * Ties together the three pieces that make drag-to-reroute work and keeps them independent
 * of the map: `DragScheduler` decides *when* to ask, the API client asks, and the pure trip
 * edits decide *what* the route becomes. Nothing here touches `google.maps`, so the whole
 * interaction is testable without an API key — the map layer only has to report where the
 * pointer is.
 *
 * The rules it exists to enforce:
 *
 * - **The grabbed leg is the only leg re-requested.** Whole-route recompute is what makes an
 *   editor feel sluggish, and on a metered engine it is what exhausts the daily quota.
 * - **One gesture inserts one via-point.** Every move re-derives the edit from the route as
 *   it was at the moment of the grab, so the cursor does not leave a trail of waypoints.
 * - **Ordering is fixed at grab time, position at release.** Where the via-point sits among
 *   its neighbours follows from where the *line* was grabbed; where it physically lands
 *   follows from where the pointer went. Recomputing the ordering mid-drag would let a
 *   via-point jump position as the user swings past another waypoint.
 * - **Only the release commits.** Mid-drag results are previews: never saved, never in undo
 *   history, and never the thing that sets the replan dirty flag.
 */
import type { ApiClient, RequestOptions } from '../api/client'
import type { Coordinate, RouteLegResponse } from '../api/types'
import { DragScheduler } from './dragScheduler'
import { insertVia, legWaypoints, spliceRoutedLeg, viaInsertionOffset, type RouteEdit } from './tripEdits'

/** Only the one call is needed, so a test double stays a one-line object. */
export type LegRouter = Pick<ApiClient, 'routeLeg'>

export interface DragSessionOptions {
  readonly client: LegRouter
  /**
   * Minimum gap between live re-routes, or `null` for preview-only.
   *
   * Comes from `GET /api/routing/capabilities` — never a constant in the frontend, or it
   * silently diverges from whichever engine is actually serving the leg.
   */
  readonly intervalMs: number | null
  /** Provisional geometry during the gesture. Never persisted. */
  readonly onPreview: (edit: RouteEdit) => void
  /** The authoritative result of the gesture. The only one that should be saved. */
  readonly onCommit: (edit: RouteEdit) => void
  readonly onError?: (error: unknown) => void
}

export interface GrabInput {
  readonly legIndex: number
  /** Where on the existing line the user took hold of it. */
  readonly grabbed: Coordinate
}

/** What a gesture needs to remember between grab and release. */
interface ActiveDrag {
  readonly base: RouteEdit
  readonly legIndex: number
  readonly offsetInLeg: number
}

/**
 * One scheduled request. The leg index travels *with* it rather than being read back from
 * the session: a release clears the gesture before its response lands, and a second gesture
 * can begin while the first is still in flight.
 */
interface LegRequest {
  readonly edit: RouteEdit
  readonly legIndex: number
}

export class DragSession {
  readonly #options: DragSessionOptions
  readonly #scheduler: DragScheduler<LegRequest, { request: LegRequest; response: RouteLegResponse }>

  #active: ActiveDrag | null = null

  constructor(options: DragSessionOptions) {
    this.#options = options
    this.#scheduler = new DragScheduler({
      intervalMs: options.intervalMs,
      route: (request, signal) => this.#routeLeg(request, signal),
      onPreview: ({ request, response }) => {
        options.onPreview(withRoutedLeg(request, response))
      },
      onCommit: ({ request, response }) => {
        options.onCommit(withRoutedLeg(request, response))
      },
      ...(options.onError === undefined ? {} : { onError: options.onError }),
    })
  }

  /**
   * Take hold of the route line.
   *
   * The insertion *order* is decided here, from the grabbed point, and then held for the
   * rest of the gesture.
   */
  begin(edit: RouteEdit, input: GrabInput): void {
    const leg = edit.legs[input.legIndex]
    if (leg === undefined) return

    this.#active = {
      base: edit,
      legIndex: input.legIndex,
      offsetInLeg: viaInsertionOffset({
        legWaypoints: legWaypoints(edit.waypoints, leg),
        geometry: leg.routed?.geometry ?? [],
        dragged: input.grabbed,
      }),
    }
  }

  /** Report a new pointer position. Subject to the provider's throttle. */
  update(to: Coordinate): void {
    const request = this.#requestFor(to)
    if (request !== null) this.#scheduler.update(request)
  }

  /** Report the release. Always routes, and is the only result that commits. */
  release(to: Coordinate): void {
    const request = this.#requestFor(to)
    if (request === null) return
    this.#active = null
    this.#scheduler.end(request)
  }

  /** Abandon the gesture: abort in-flight work and deliver nothing further. */
  cancel(): void {
    this.#active = null
    this.#scheduler.cancel()
  }

  /** The route as it would be with the via-point at `to`, derived afresh from the grab. */
  #requestFor(to: Coordinate): LegRequest | null {
    const active = this.#active
    if (active === null) return null // a move or release with no grab behind it
    return {
      legIndex: active.legIndex,
      edit: insertVia(active.base, {
        legIndex: active.legIndex,
        offsetInLeg: active.offsetInLeg,
        coordinate: to,
      }),
    }
  }

  async #routeLeg(
    request: LegRequest,
    signal: AbortSignal,
  ): Promise<{ request: LegRequest; response: RouteLegResponse }> {
    const leg = request.edit.legs[request.legIndex]
    if (leg === undefined) throw new RangeError(`no leg at index ${String(request.legIndex)}`)

    const options: RequestOptions = { signal }
    const response = await this.#options.client.routeLeg(
      {
        waypoints: legWaypoints(request.edit.waypoints, leg),
        // The leg's own policy governs: a drag must not convert a technical off-road leg
        // into a highway connector.
        intent: leg.intent,
        ...(leg.provider_override === null || leg.provider_override === undefined
          ? {}
          : { provider_override: leg.provider_override }),
      },
      options,
    )
    return { request, response }
  }
}

function withRoutedLeg(request: LegRequest, response: RouteLegResponse): RouteEdit {
  return {
    ...request.edit,
    legs: spliceRoutedLeg(request.edit.legs, request.legIndex, response.leg),
  }
}
