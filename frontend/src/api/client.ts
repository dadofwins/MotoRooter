/**
 * The typed API client. Every network call the frontend makes goes through here.
 *
 * Three properties are load-bearing:
 *
 * 1. **No hand-written shapes.** Requests and responses are the types generated from the
 *    backend's OpenAPI document (via `./types`). A shape that disagrees with the backend
 *    is a build failure, not a runtime surprise.
 * 2. **Every call takes an `AbortSignal`.** The fast path supersedes its own requests
 *    while the user drags; `DragScheduler` can only abort what the client threads through.
 *    Aborts propagate unwrapped so the scheduler still recognises them.
 * 3. **Failures are typed by cause.** `ApiNotImplementedError` for the endpoints that are
 *    still stubs, `ApiNetworkError` for a request that never landed, `ApiError` with a
 *    stable `code` for everything the server refused.
 *
 * The app is served from the same origin as the API in production, so the default base URL
 * is empty and paths are root-relative. Vite proxies `/api` to the backend in dev.
 */
import { ApiError, ApiNetworkError, ApiNotImplementedError, isAbortError, type ErrorCode } from './errors'
import type {
  RouteThroughBestRequest,
  RouteThroughBestResponse,
  GeocodeResponse,
  Coordinate,
  ChatEvent,
  ChatRequest,
  CreateTripRequest,
  HealthResponse,
  PoiCategory,
  PoiDetailResponse,
  ReplanEvent,
  ReplanRequest,
  RouteLegInput,
  RouteLegResponse,
  RoutingCapabilitiesResponse,
  Trip,
  TripBlurbRequest,
  TripBlurbResponse,
  TripSummary,
  UpdateTripRequest,
} from './types'

/** The slice of `fetch` this client uses. Injected in tests; never stubbed globally. */
export type FetchLike = (url: string, init: RequestInit) => Promise<Response>

export interface RequestOptions {
  /**
   * Cancels the request. Mandatory in practice on the fast path: a superseded drag
   * request must be abandoned, both to keep stale geometry off the map and to stop it
   * spending provider quota.
   */
  readonly signal?: AbortSignal | undefined
}

export interface ApiClientOptions {
  /** Prefix for every path. Empty (same origin) by default; a trailing slash is fine. */
  readonly baseUrl?: string
  readonly fetch?: FetchLike
}

export interface ApiClient {
  /** Liveness plus the routing providers this deployment actually registered. */
  health(options?: RequestOptions): Promise<HealthResponse>
  /**
   * Provider and per-intent routing capabilities.
   *
   * The only legitimate source of `live_update_interval_ms` — the drag throttle must never
   * be a constant in the frontend, or it silently diverges from the engine serving the leg.
   */
  routingCapabilities(options?: RequestOptions): Promise<RoutingCapabilitiesResponse>
  /**
   * Resolve a typed place name to real places.
   *
   * Several results, not one: a name is a claim until something verifies it, and plenty of names
   * verify to more than one real place. Choosing silently is the failure this exists to avoid, so
   * the caller shows the list. An empty `results` is an ordinary answer — a typo matches nothing.
   *
   * `near` biases toward a point, which is what makes "Leavenworth" the Washington one on a trip
   * already in Washington. Omitted rather than invented when the trip has nowhere to bias toward.
   */
  geocode(
    query: string,
    options?: RequestOptions & { readonly near?: Coordinate },
  ): Promise<GeocodeResponse>
  /** Fast path: route one leg. No LLM, no persistence, sub-second. */
  routeLeg(request: RouteLegInput, options?: RequestOptions): Promise<RouteLegResponse>

  listTrips(options?: RequestOptions): Promise<TripSummary[]>
  createTrip(request: CreateTripRequest, options?: RequestOptions): Promise<Trip>
  getTrip(slug: string, options?: RequestOptions): Promise<Trip>
  /** Full replacement, not a patch — the frontend holds the authoritative trip state. */
  updateTrip(slug: string, request: UpdateTripRequest, options?: RequestOptions): Promise<Trip>
  deleteTrip(slug: string, options?: RequestOptions): Promise<void>

  /**
   * Slow path: discovery and enrichment, streamed as progress events.
   *
   * Stubbed today — the first `next()` rejects with `ApiNotImplementedError`. Iterating it
   * is already the right call shape, so nothing here changes when the backend lands.
   */
  replan(
    slug: string,
    request: ReplanRequest,
    options?: RequestOptions,
  ): AsyncGenerator<ReplanEvent, void, undefined>
  /**
   * One turn of conversation about a trip, streamed as it happens.
   *
   * The trip is addressed by slug rather than sent, so the assistant reads and edits the same
   * document the mouse does — chat is a second path to existing functions, never a separate
   * model of the trip. Framing is NDJSON, identical to replan.
   */
  chat(
    slug: string,
    request: ChatRequest,
    options?: RequestOptions,
  ): AsyncGenerator<ChatEvent, void, undefined>
  /**
   * Put the best-ranked discovered places on the route, in riding order.
   *
   * Separate from discovery because the judgement is persisted on the POIs, so this needs no
   * search — a rider can change their mind without paying for sixty seconds of discovery again.
   * Ranks and caps rather than taking everything above a threshold: the scores' *ordering* is
   * stable between runs but the count above any given line is not, so a threshold would give two
   * different answers to the same question.
   */
  routeThroughBest(
    slug: string,
    request: RouteThroughBestRequest,
    options?: RequestOptions,
  ): Promise<RouteThroughBestResponse>
  /** GPX track plus ordered waypoints. Stubbed today (`ApiNotImplementedError`). */
  exportGpx(slug: string, options?: RequestOptions): Promise<Blob>
  /**
   * One short line characterising the trip, for the rail header.
   *
   * Decoration. `blurb` is null whenever the backend produced nothing usable, and that is an
   * ordinary answer rather than a failure — a caller treats null and a 501 identically and
   * keeps whatever header it was already showing. Nothing may wait on this.
   */
  tripBlurb(
    slug: string,
    request: TripBlurbRequest,
    options?: RequestOptions,
  ): Promise<TripBlurbResponse>
  /**
   * Everything Places knows about one place.
   *
   * Display data for the POI dialog, stubbed today (`ApiNotImplementedError`). Response-only:
   * Google's terms forbid persisting anything here but `place_id`.
   *
   * `category` is the one the caller already holds, and the endpoint uses it **only** where
   * Places' own types map to nothing — it never overrides what Places says. Without it such a
   * place is a 500: the server has no way to classify it and the client had no way to help,
   * which is how "detail for this place could not be loaded" appeared for places that plainly
   * exist. Optional because a caller without a category should send none rather than guess.
   */
  placeDetail(
    placeId: string,
    options?: RequestOptions & { readonly category?: PoiCategory },
  ): Promise<PoiDetailResponse>
}

interface SendInit {
  readonly method: 'GET' | 'POST' | 'PUT' | 'DELETE'
  /** JSON request body. Omitted entirely rather than sent as `null` when absent. */
  readonly json?: unknown
  readonly accept?: string
}

const JSON_TYPE = 'application/json'

/**
 * Cap on one unframed streamed line. Generous next to any real `ReplanEvent` — a discovery
 * stage carrying dozens of POIs is kilobytes — and small enough that an unframed body
 * fails fast instead of consuming the tab.
 */
const MAX_STREAM_LINE_BYTES = 1024 * 1024

/** Percent-encodes one path segment, so a slug can never widen the URL it appears in. */
function segment(value: string): string {
  return encodeURIComponent(value)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function parseJson(text: string): unknown {
  return JSON.parse(text) as unknown
}

/** Reads an error body without ever throwing: a failure response may be anything at all. */
async function readErrorBody(response: Response): Promise<unknown> {
  let text: string
  try {
    text = await response.text()
  } catch (error) {
    if (isAbortError(error)) throw error
    return undefined
  }
  if (text === '') return undefined
  try {
    return parseJson(text)
  } catch {
    return text // HTML from a proxy, a plain-text gateway message, a truncated body
  }
}

function codeFrom(body: unknown, status: number): ErrorCode {
  const code = isRecord(body) ? body['code'] : undefined
  if (typeof code === 'string' && code !== '') return code

  // No `code` in the body, so derive the most specific one the status justifies. The
  // backend now sends a `code` with every error, including 501s and rejected request
  // bodies, so reaching here means the response did not come from the app — an
  // intermediary, or a proxy error page. Keep the mapping anyway: a legible code costs
  // nothing and the alternative is a component with no branch to take.
  if (status === 501) return 'not_implemented'
  if (status === 422) return 'validation_error'
  return 'unknown_error'
}

function detailFrom(body: unknown, status: number): string {
  const detail = isRecord(body) ? body['detail'] : undefined
  if (typeof detail === 'string' && detail !== '') return detail
  if (status === 422) return 'The request was rejected as invalid.'
  return `The server returned HTTP ${status}.`
}

async function errorFor(response: Response): Promise<ApiError> {
  const body = await readErrorBody(response)
  const detail = detailFrom(body, response.status)
  if (response.status === 501) return new ApiNotImplementedError({ detail, body })
  return new ApiError({
    status: response.status,
    code: codeFrom(body, response.status),
    detail,
    body,
  })
}

/**
 * Decodes a success body.
 *
 * The contract says this is JSON of shape `T`, and the generated types are trusted rather
 * than re-validated at runtime. What is *not* trusted is that the response came from the
 * app at all: a proxy answering 200 with an HTML page would otherwise surface as a bare
 * `SyntaxError`, which no component can sensibly handle.
 */
/**
 * A body that could not be read to completion — a stream that errored mid-download.
 *
 * Aborts pass through untouched; everything else becomes an `ApiNetworkError`, because the
 * request did reach the server but the response never arrived in full.
 */
function bodyReadFailure(error: unknown): never {
  if (isAbortError(error)) throw error
  throw new ApiNetworkError({ detail: 'The response body could not be read.', cause: error })
}

/**
 * A body that arrived intact but was not the JSON it claimed to be.
 *
 * Every JSON decode in this module goes through here. A bare `SyntaxError` reaching a
 * component is unclassifiable — `isApiError` returns false and the "coming soon" and
 * "quota exceeded" branches alike are skipped — so there must be no exceptions, including
 * on the streaming path.
 */
function malformedJson(status: number, text: string, error: unknown): never {
  throw new ApiError({
    status,
    code: 'unknown_error',
    detail: 'The server sent a response that was not valid JSON.',
    body: text,
    cause: error,
  })
}

async function readJson<T>(response: Response): Promise<T> {
  let text: string
  try {
    text = await response.text()
  } catch (error) {
    bodyReadFailure(error)
  }
  try {
    return parseJson(text) as T
  } catch (error) {
    malformedJson(response.status, text, error)
  }
}

/**
 * Yields one value per newline-delimited JSON object in the response body.
 *
 * A chunk boundary lands mid-line often enough that buffering is not optional. Breaking
 * out of the loop early cancels the body, so abandoning a replan stops the download.
 */
async function* streamNdjson<T>(response: Response): AsyncGenerator<T, void, undefined> {
  const body = response.body
  if (body === null) return

  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  /** Same envelope as `readJson`: no decode failure escapes as a raw `SyntaxError`. */
  const parseLine = (line: string): T => {
    try {
      return parseJson(line) as T
    } catch (error) {
      malformedJson(response.status, line, error)
    }
  }

  try {
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>
      try {
        chunk = await reader.read()
      } catch (error) {
        bodyReadFailure(error)
      }
      if (chunk.done) break

      buffer += decoder.decode(chunk.value, { stream: true })
      for (let newline = buffer.indexOf('\n'); newline !== -1; newline = buffer.indexOf('\n')) {
        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        if (line.trim() !== '') yield parseLine(line)
      }

      // An unframed body — a proxy answering 200 with a page, or a stream that never
      // emits a newline — must not be accumulated until the tab dies.
      if (buffer.length > MAX_STREAM_LINE_BYTES) {
        throw new ApiError({
          status: response.status,
          code: 'unknown_error',
          detail: `A single streamed line exceeded ${String(MAX_STREAM_LINE_BYTES)} bytes, so the response is too large to be an event stream.`,
          body: buffer.slice(0, 512),
        })
      }
    }

    buffer += decoder.decode()
    // The last line of a well-formed stream may be unterminated; a truncated one looks
    // exactly the same here, and parseLine is what tells them apart.
    if (buffer.trim() !== '') yield parseLine(buffer)
  } finally {
    await reader.cancel().catch(() => undefined)
  }
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '')
  const doFetch: FetchLike = options.fetch ?? ((url, init) => globalThis.fetch(url, init))

  async function send(path: string, init: SendInit, request?: RequestOptions): Promise<Response> {
    const headers: Record<string, string> = { accept: init.accept ?? JSON_TYPE }
    let body: string | undefined
    if (init.json !== undefined) {
      headers['content-type'] = JSON_TYPE
      body = JSON.stringify(init.json)
    }

    let response: Response
    try {
      response = await doFetch(`${baseUrl}${path}`, {
        method: init.method,
        headers,
        // `null` rather than `undefined`: exactOptionalPropertyTypes forbids assigning
        // undefined to an optional property, and fetch treats both the same way.
        signal: request?.signal ?? null,
        ...(body === undefined ? {} : { body }),
      })
    } catch (error) {
      // An abort is not a failure — the caller asked for it. Rethrow it unchanged so
      // DragScheduler's abort check still matches.
      if (isAbortError(error)) throw error
      throw new ApiNetworkError({
        detail: error instanceof Error ? error.message : 'The server could not be reached.',
        cause: error,
      })
    }

    if (!response.ok) throw await errorFor(response)
    return response
  }

  return {
    async health(requestOptions?: RequestOptions): Promise<HealthResponse> {
      return readJson<HealthResponse>(await send('/api/health', { method: 'GET' }, requestOptions))
    },

    async geocode(
      query: string,
      requestOptions?: RequestOptions & { readonly near?: Coordinate },
    ): Promise<GeocodeResponse> {
      const params = new URLSearchParams({ q: query })
      const near = requestOptions?.near
      if (near !== undefined) params.set('near', `${String(near.lat)},${String(near.lon)}`)
      return readJson<GeocodeResponse>(
        await send(`/api/geocode?${params.toString()}`, { method: 'GET' }, requestOptions),
      )
    },

    async routingCapabilities(
      requestOptions?: RequestOptions,
    ): Promise<RoutingCapabilitiesResponse> {
      return readJson<RoutingCapabilitiesResponse>(
        await send('/api/routing/capabilities', { method: 'GET' }, requestOptions),
      )
    },

    async routeLeg(
      request: RouteLegInput,
      requestOptions?: RequestOptions,
    ): Promise<RouteLegResponse> {
      return readJson<RouteLegResponse>(
        await send('/api/routing/leg', { method: 'POST', json: request }, requestOptions),
      )
    },

    async listTrips(requestOptions?: RequestOptions): Promise<TripSummary[]> {
      return readJson<TripSummary[]>(await send('/api/trips', { method: 'GET' }, requestOptions))
    },

    async createTrip(request: CreateTripRequest, requestOptions?: RequestOptions): Promise<Trip> {
      return readJson<Trip>(
        await send('/api/trips', { method: 'POST', json: request }, requestOptions),
      )
    },

    async getTrip(slug: string, requestOptions?: RequestOptions): Promise<Trip> {
      return readJson<Trip>(
        await send(`/api/trips/${segment(slug)}`, { method: 'GET' }, requestOptions),
      )
    },

    async updateTrip(
      slug: string,
      request: UpdateTripRequest,
      requestOptions?: RequestOptions,
    ): Promise<Trip> {
      return readJson<Trip>(
        await send(
          `/api/trips/${segment(slug)}`,
          { method: 'PUT', json: request },
          requestOptions,
        ),
      )
    },

    async deleteTrip(slug: string, requestOptions?: RequestOptions): Promise<void> {
      // 204, so there is no body to parse — calling .json() on it would reject.
      await send(`/api/trips/${segment(slug)}`, { method: 'DELETE' }, requestOptions)
    },

    async *replan(
      slug: string,
      request: ReplanRequest,
      requestOptions?: RequestOptions,
    ): AsyncGenerator<ReplanEvent, void, undefined> {
      const response = await send(
        `/api/trips/${segment(slug)}/replan`,
        { method: 'POST', json: request, accept: `application/x-ndjson, ${JSON_TYPE}` },
        requestOptions,
      )
      yield* streamNdjson<ReplanEvent>(response)
    },

    async *chat(
      slug: string,
      request: ChatRequest,
      requestOptions?: RequestOptions,
    ): AsyncGenerator<ChatEvent, void, undefined> {
      const response = await send(
        `/api/trips/${segment(slug)}/chat`,
        { method: 'POST', json: request, accept: `application/x-ndjson, ${JSON_TYPE}` },
        requestOptions,
      )
      yield* streamNdjson<ChatEvent>(response)
    },

    async routeThroughBest(
      slug: string,
      request: RouteThroughBestRequest,
      requestOptions?: RequestOptions,
    ): Promise<RouteThroughBestResponse> {
      return readJson<RouteThroughBestResponse>(
        await send(
          `/api/trips/${segment(slug)}/route-through-best`,
          { method: 'POST', json: request },
          requestOptions,
        ),
      )
    },

    async tripBlurb(
      slug: string,
      request: TripBlurbRequest,
      requestOptions?: RequestOptions,
    ): Promise<TripBlurbResponse> {
      return readJson<TripBlurbResponse>(
        await send(
          `/api/trips/${segment(slug)}/blurb`,
          { method: 'POST', json: request },
          requestOptions,
        ),
      )
    },

    async exportGpx(slug: string, requestOptions?: RequestOptions): Promise<Blob> {
      const response = await send(
        `/api/trips/${segment(slug)}/gpx`,
        { method: 'GET', accept: `application/gpx+xml, ${JSON_TYPE}` },
        requestOptions,
      )
      try {
        return await response.blob()
      } catch (error) {
        // Not JSON, but a body read can fail here exactly as it can anywhere else, and a
        // caller branching on isApiError would otherwise drop it.
        bodyReadFailure(error)
      }
    },

    async placeDetail(
      placeId: string,
      requestOptions?: RequestOptions & { readonly category?: PoiCategory },
    ): Promise<PoiDetailResponse> {
      const category = requestOptions?.category
      const query = category === undefined ? '' : `?${new URLSearchParams({ category }).toString()}`
      return readJson<PoiDetailResponse>(
        await send(`/api/places/${segment(placeId)}${query}`, { method: 'GET' }, requestOptions),
      )
    },
  }
}
