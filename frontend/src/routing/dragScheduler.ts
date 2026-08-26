/**
 * Schedules routing requests during a route drag.
 *
 * Three guarantees, each of which a naive implementation gets wrong:
 *
 * 1. **Throttled, not debounced.** A leading-edge throttle keeps the line moving while
 *    the drag is in motion; debounce would show nothing until the user pauses. The
 *    interval comes from the resolved provider's capabilities, so a cheap engine can
 *    refresh near-live while a metered one holds back. `intervalMs: null` means
 *    preview-only — rubber-band locally and issue nothing until release.
 * 2. **Release always commits.** The drag-end request is unconditional and authoritative,
 *    which is what guarantees the final geometry actually connects every point.
 * 3. **Stale responses are discarded.** Every request carries a monotonic sequence number;
 *    anything older than the newest result is dropped, and superseded requests are
 *    aborted. Without this, a slow mid-drag response landing after the commit silently
 *    reverts the user's edit.
 */

export interface DragSchedulerOptions<Req, Res> {
  /**
   * Minimum gap between live updates, or `null` for preview-only.
   *
   * The starting value only. Cadence belongs to the engine serving the leg being dragged, and
   * a trip's legs no longer share one engine, so it is re-resolved per gesture — see
   * `setIntervalMs`.
   */
  intervalMs: number | null
  route: (request: Req, signal: AbortSignal) => Promise<Res>
  /** Provisional mid-drag geometry. Never persisted, never added to undo history. */
  onPreview: (result: Res) => void
  /** Authoritative post-release geometry. The only result that should be saved. */
  onCommit: (result: Res) => void
  onError?: (error: unknown) => void
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

export class DragScheduler<Req, Res> {
  readonly #options: DragSchedulerOptions<Req, Res>

  #intervalMs: number | null

  #sequence = 0
  /** Highest sequence whose result has been delivered; anything older is stale. */
  #latestDelivered = -1
  #inFlight: AbortController | null = null

  #lastDispatchAt: number | null = null
  #timer: ReturnType<typeof setTimeout> | null = null
  #queued: Req | null = null

  constructor(options: DragSchedulerOptions<Req, Res>) {
    this.#options = options
    this.#intervalMs = options.intervalMs
  }

  /**
   * Set the cadence for the next gesture.
   *
   * Called between gestures rather than during one: which engine is being metered depends on
   * which leg the rider grabbed, and a trip's legs can be served by different engines. Changing
   * it mid-gesture would be a cadence the rider's current drag never agreed to.
   */
  setIntervalMs(intervalMs: number | null): void {
    this.#intervalMs = intervalMs
  }

  /** Report a new drag position. Subject to the throttle. */
  update(request: Req): void {
    if (this.#intervalMs === null) return // preview-only provider

    const now = Date.now()
    const elapsed = this.#lastDispatchAt === null ? Infinity : now - this.#lastDispatchAt

    if (elapsed >= this.#intervalMs) {
      this.#dispatch(request, false)
      return
    }

    // Inside the window: keep only the newest position and fire it on the trailing edge.
    this.#queued = request
    if (this.#timer === null) {
      this.#timer = setTimeout(() => {
        this.#timer = null
        const queued = this.#queued
        this.#queued = null
        if (queued !== null) this.#dispatch(queued, false)
      }, this.#intervalMs - elapsed)
    }
  }

  /** Report the drag release. Always routes, bypassing the throttle, and commits. */
  end(request: Req): void {
    this.#clearPending()
    this.#lastDispatchAt = null // next drag starts with a fresh window
    this.#dispatch(request, true)
  }

  /** Abandon the drag: abort in-flight work and deliver nothing further. */
  cancel(): void {
    this.#clearPending()
    this.#inFlight?.abort()
    this.#inFlight = null
    this.#latestDelivered = this.#sequence
    this.#lastDispatchAt = null
  }

  #clearPending(): void {
    if (this.#timer !== null) {
      clearTimeout(this.#timer)
      this.#timer = null
    }
    this.#queued = null
  }

  #dispatch(request: Req, isCommit: boolean): void {
    this.#inFlight?.abort()

    const controller = new AbortController()
    const sequence = ++this.#sequence
    this.#inFlight = controller
    // A commit ends the gesture, so it must not start a throttle window — the next
    // drag is a new gesture and deserves an immediate leading-edge update.
    this.#lastDispatchAt = isCommit ? null : Date.now()

    void this.#options
      .route(request, controller.signal)
      .then((result) => {
        // Drop anything superseded while it was in flight.
        if (sequence <= this.#latestDelivered || controller.signal.aborted) return
        this.#latestDelivered = sequence
        if (isCommit) this.#options.onCommit(result)
        else this.#options.onPreview(result)
      })
      .catch((error: unknown) => {
        if (isAbortError(error) || controller.signal.aborted) return
        this.#options.onError?.(error)
      })
  }
}
