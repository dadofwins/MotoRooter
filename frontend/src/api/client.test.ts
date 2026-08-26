import { describe, expect, expectTypeOf, it, vi } from 'vitest'
import {
  intentRouting,
  providerCapabilities,
  routeLeg,
  routeLegResponse,
  trip as tripFixture,
  tripSummary,
} from './fixtures'
import { createApiClient, type FetchLike } from './client'
import { ApiError, ApiNetworkError, ApiNotImplementedError } from './errors'
import { DragScheduler } from '../routing/dragScheduler'
import type {
  HealthResponse,
  PoiDetailResponse,
  ReplanEvent,
  RouteLegResponse,
  RoutingCapabilitiesResponse,
  Trip,
  TripSummary,
} from './types'

/**
 * Every test mocks at the fetch boundary. Nothing here touches a server — the live
 * contract is guarded by `make contract-check` and the compile-time assertions in
 * `types.test.ts`, not by network calls in a unit test.
 */

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** A fetch stub that replays the given responses in order. */
function stubFetch(...responses: Response[]): ReturnType<typeof vi.fn<FetchLike>> {
  const fetchMock = vi.fn<FetchLike>()
  for (const response of responses) fetchMock.mockResolvedValueOnce(response)
  return fetchMock
}

function lastCall(fetchMock: ReturnType<typeof vi.fn<FetchLike>>): [string, RequestInit] {
  const call = fetchMock.mock.lastCall
  if (call === undefined) throw new Error('fetch was never called')
  return call
}

/** The decoded JSON body of the most recent request. */
function sentJson(fetchMock: ReturnType<typeof vi.fn<FetchLike>>): Record<string, unknown> {
  const body = lastCall(fetchMock)[1].body
  if (typeof body !== 'string') throw new Error('request carried no JSON body')
  return JSON.parse(body) as Record<string, unknown>
}

type Api = ReturnType<typeof createApiClient>

/** A 200 NDJSON response whose body arrives as the given chunks. */
function ndjsonResponse(chunks: readonly string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, {
    status: 200,
    headers: { 'content-type': 'application/x-ndjson' },
  })
}

/** A 200 response whose body fails partway through being read. */
function brokenBodyResponse(): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.error(new TypeError('network error while reading the body'))
    },
  })
  return new Response(body, { status: 200 })
}

/**
 * The shared builders, not local copies.
 *
 * This file kept its own `DERIVED_METRICS`, `TRIP`, `TRIP_SUMMARY` and leg literal, which is
 * how a contract addition broke four files at once instead of one: `duration_is_estimated`
 * and `duration_is_trustworthy` had to be added everywhere a shape was written out by hand.
 * A builder per shape means the next field is one edit in `fixtures.ts`.
 */
const TRIP: Trip = tripFixture()

const TRIP_SUMMARY: TripSummary = tripSummary()

const LEG_RESPONSE: RouteLegResponse = routeLegResponse({
  leg: routeLeg({
    geometry: [
      { lat: 47.6, lon: -120.7 },
      { lat: 47.7, lon: -120.6 },
    ],
    distance_m: 14_200,
    duration_s: 900,
  }),
  live_update_interval_ms: 3000,
  estimated_duration_s: 900,
})

describe('request shape', () => {
  it('GETs health against the same origin by default', async () => {
    const health: HealthResponse = { status: 'ok', providers: ['fake'] }
    const fetchMock = stubFetch(json(health))
    const api = createApiClient({ fetch: fetchMock })

    await expect(api.health()).resolves.toEqual(health)

    const [url, init] = lastCall(fetchMock)
    expect(url).toBe('/api/health')
    expect(init.method).toBe('GET')
  })

  it('prefixes an explicit baseUrl and tolerates a trailing slash', async () => {
    const fetchMock = stubFetch(json({ status: 'ok', providers: [] }))
    const api = createApiClient({ baseUrl: 'https://motorooter.example/', fetch: fetchMock })

    await api.health()

    expect(lastCall(fetchMock)[0]).toBe('https://motorooter.example/api/health')
  })

  it('sends a JSON body with a JSON content type on writes', async () => {
    const fetchMock = stubFetch(json(TRIP, 201))
    const api = createApiClient({ fetch: fetchMock })

    await api.createTrip({ name: 'WABDR North' })

    const [url, init] = lastCall(fetchMock)
    expect(url).toBe('/api/trips')
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('content-type')).toBe('application/json')
    expect(init.body).toBe(JSON.stringify({ name: 'WABDR North' }))
  })

  it('sends no body and no content type on reads', async () => {
    const fetchMock = stubFetch(json(TRIP))
    const api = createApiClient({ fetch: fetchMock })

    await api.getTrip('wabdr-north')

    const [, init] = lastCall(fetchMock)
    expect(init.body).toBeUndefined()
    expect(new Headers(init.headers).get('content-type')).toBeNull()
  })

  it('escapes path segments, so a slug can never climb out of its route', async () => {
    const fetchMock = stubFetch(json(TRIP, 404), json(TRIP))
    const api = createApiClient({ fetch: fetchMock })

    // A slug this malformed is rejected by the backend; what matters is that the client
    // asks about *that* slug rather than silently requesting a different resource.
    await api.getTrip('../../etc/passwd').catch(() => undefined)
    expect(lastCall(fetchMock)[0]).toBe('/api/trips/..%2F..%2Fetc%2Fpasswd')

    await api.placeDetail('places/ChIJ 1+2').catch(() => undefined)
    expect(lastCall(fetchMock)[0]).toBe('/api/places/places%2FChIJ%201%2B2')
  })
})

describe('endpoints that exist today', () => {
  it('reads routing capabilities, which is where drag throttling comes from', async () => {
    const capabilities: RoutingCapabilitiesResponse = {
      providers: [
        providerCapabilities({
          name: 'ors',
          prefers_unpaved: true,
          // Distinct from prefers_unpaved: that is what the engine will route *onto*,
          // this is what it can *tell you* about the result. Google is false for both
          // while meaning quite different things by each.
          reports_surface: true,
          alternatives: true,
          elevation: true,
          live_update_interval_ms: 3000,
          daily_quota: 2000,
        }),
      ],
      intents: {
        unpaved: intentRouting({ provider: 'ors', live_update_interval_ms: 3000 }),
        highway_connector: intentRouting({ provider: 'google', live_update_interval_ms: 1000 }),
      },
    }
    const fetchMock = stubFetch(json(capabilities))
    const api = createApiClient({ fetch: fetchMock })

    const result = await api.routingCapabilities()

    expect(lastCall(fetchMock)[0]).toBe('/api/routing/capabilities')
    expect(result.intents['unpaved']?.live_update_interval_ms).toBe(3000)
  })

  it('routes one leg and returns the throttle budget alongside the geometry', async () => {
    const fetchMock = stubFetch(json(LEG_RESPONSE))
    const api = createApiClient({ fetch: fetchMock })

    const result = await api.routeLeg({
      waypoints: [
        { lat: 47.6, lon: -120.7 },
        { lat: 47.7, lon: -120.6 },
      ],
      intent: 'unpaved',
    })

    const [url, init] = lastCall(fetchMock)
    expect(url).toBe('/api/routing/leg')
    expect(init.method).toBe('POST')
    expect(result.leg.provider).toBe('fake')
    expect(result.live_update_interval_ms).toBe(3000)
  })

  it('omits routing flags the caller left out rather than guessing their defaults', async () => {
    const fetchMock = stubFetch(json(LEG_RESPONSE))
    const api = createApiClient({ fetch: fetchMock })

    await api.routeLeg({
      waypoints: [
        { lat: 47.6, lon: -120.7 },
        { lat: 47.7, lon: -120.6 },
      ],
      intent: 'twisty_paved',
    })

    // The defaults for avoid_tolls and friends live in the backend schema. Sending our
    // own copy of them would silently freeze today's values into the client.
    expect(Object.keys(sentJson(fetchMock)).sort()).toEqual(['intent', 'waypoints'])
  })

  it('lists trips', async () => {
    const fetchMock = stubFetch(json([TRIP_SUMMARY]))
    const api = createApiClient({ fetch: fetchMock })

    await expect(api.listTrips()).resolves.toEqual([TRIP_SUMMARY])
    expect(lastCall(fetchMock)[0]).toBe('/api/trips')
  })

  it('creates, reads and replaces a trip', async () => {
    const fetchMock = stubFetch(json(TRIP, 201), json(TRIP), json(TRIP))
    const api = createApiClient({ fetch: fetchMock })

    await expect(api.createTrip({ name: 'WABDR North' })).resolves.toEqual(TRIP)
    await expect(api.getTrip('wabdr-north')).resolves.toEqual(TRIP)
    await expect(api.updateTrip('wabdr-north', { name: 'WABDR North (v2)' })).resolves.toEqual(TRIP)

    expect(fetchMock.mock.calls.map(([url, init]) => `${String(init.method)} ${url}`)).toEqual([
      'POST /api/trips',
      'GET /api/trips/wabdr-north',
      'PUT /api/trips/wabdr-north',
    ])
  })

  it('deletes a trip without trying to parse the empty 204 body', async () => {
    const fetchMock = stubFetch(new Response(null, { status: 204 }))
    const api = createApiClient({ fetch: fetchMock })

    // A 204 has no body at all; calling .json() on it rejects.
    await expect(api.deleteTrip('wabdr-north')).resolves.toBeUndefined()
    expect(lastCall(fetchMock)[1].method).toBe('DELETE')
  })
})

describe('endpoints that are still stubs', () => {
  it('turns the replan 501 into a typed "not implemented" the UI can label', async () => {
    const fetchMock = stubFetch(json({ detail: 'replan is not implemented yet' }, 501))
    const api = createApiClient({ fetch: fetchMock })

    const events = api.replan('wabdr-north', { prompt: 'prefer hot springs' })
    const error = await events.next().then(
      () => undefined,
      (caught: unknown) => caught,
    )

    expect(error).toBeInstanceOf(ApiNotImplementedError)
    expect((error as ApiError).code).toBe('not_implemented')
    expect((error as ApiError).detail).toBe('replan is not implemented yet')
  })

  it('turns the GPX 501 into the same typed stub error', async () => {
    const fetchMock = stubFetch(json({ detail: 'GPX export is not implemented yet' }, 501))
    const api = createApiClient({ fetch: fetchMock })

    await expect(api.exportGpx('wabdr-north')).rejects.toBeInstanceOf(ApiNotImplementedError)
  })

  it('turns the place detail 501 into the same typed stub error', async () => {
    const fetchMock = stubFetch(json({ detail: 'Places enrichment is not implemented yet' }, 501))
    const api = createApiClient({ fetch: fetchMock })

    await expect(api.placeDetail('ChIJ123')).rejects.toBeInstanceOf(ApiNotImplementedError)
  })

  it('distinguishes a 501 stub from a genuine failure on the same endpoint', async () => {
    const fetchMock = stubFetch(json({ code: 'trip_not_found', detail: 'no trip named x' }, 404))
    const api = createApiClient({ fetch: fetchMock })

    const error = await api.exportGpx('x').catch((caught: unknown) => caught)

    // The backend checks the trip exists before raising 501, so 404 here is real.
    expect(error).toBeInstanceOf(ApiError)
    expect(error).not.toBeInstanceOf(ApiNotImplementedError)
    expect((error as ApiError).code).toBe('trip_not_found')
  })

  it('returns real data through the frozen shapes once the stub is replaced', async () => {
    // Same call sites, no client change: this is what the frozen schemas buy.
    const detail: PoiDetailResponse = {
      detail: {
        poi: {
          id: 'poi-1',
          name: 'Sun Mountain Lodge',
          category: 'hotel',
          coordinate: { lat: 48.5, lon: -120.2 },
          source: 'places',
          place_id: 'ChIJ123',
          note: null,
          on_route: false,
        },
        rating: 4.6,
        user_rating_count: 812,
        photo_urls: [],
        opening_hours: [],
        reviews: [],
        phone: null,
        website: null,
      },
    }
    const fetchMock = stubFetch(json(detail))
    const api = createApiClient({ fetch: fetchMock })

    await expect(api.placeDetail('ChIJ123')).resolves.toEqual(detail)
  })

  it('reports a malformed stream line as an ApiError, not a bare SyntaxError', async () => {
    // The stream was the one path where a JSON.parse failure could still escape. A single
    // bad line must not surface as an error no component can classify.
    const fetchMock = stubFetch(ndjsonResponse(['{"stage":"route_search","message":"ok"}\n', 'not json\n']))
    const api = createApiClient({ fetch: fetchMock })

    const events = api.replan('wabdr-north', {})
    expect((await events.next()).value?.stage).toBe('route_search')

    const error = await events.next().then(
      () => undefined,
      (caught: unknown) => caught,
    )
    expect(error).toBeInstanceOf(ApiError)
    expect(error).not.toBeInstanceOf(SyntaxError)
    expect((error as ApiError).code).toBe('unknown_error')
  })

  it('reports a truncated final line as an ApiError too', async () => {
    // A connection dropped mid-event leaves an unterminated partial object behind.
    const fetchMock = stubFetch(ndjsonResponse(['{"stage":"discovery","mess']))
    const api = createApiClient({ fetch: fetchMock })

    await expect(api.replan('x', {}).next()).rejects.toBeInstanceOf(ApiError)
  })

  it('reports an HTML body on the stream endpoint like every other endpoint does', async () => {
    const fetchMock = stubFetch(ndjsonResponse(['<html><body>502</body></html>\n']))
    const api = createApiClient({ fetch: fetchMock })

    const error = (await api
      .replan('x', {})
      .next()
      .catch((caught: unknown) => caught)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.body).toContain('<html>')
  })

  it('refuses to buffer an unbounded line rather than growing until the tab dies', async () => {
    // A 200 body with no newline in it — a misbehaving proxy, or a stream that never
    // frames — would otherwise be accumulated in full.
    const megabyte = 'x'.repeat(1024 * 1024)
    const fetchMock = stubFetch(ndjsonResponse([megabyte, megabyte, megabyte]))
    const api = createApiClient({ fetch: fetchMock })

    const error = (await api
      .replan('x', {})
      .next()
      .catch((caught: unknown) => caught)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.detail).toMatch(/too large|too long/i)
  })

  it('streams replan events line by line, including across chunk boundaries', async () => {
    // Events arrive as newline-delimited JSON, so a chunk can split a line in half.
    const chunks = [
      '{"stage":"route_search","message":"routing legs","progress":0.2}\n{"stage":"disc',
      'overy","message":"found 3 camps","progress":0.6}\n\n',
      '{"stage":"done","message":"replan complete","progress":1}\n',
    ]
    const encoder = new TextEncoder()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
        controller.close()
      },
    })
    const fetchMock = stubFetch(
      new Response(body, { status: 200, headers: { 'content-type': 'application/x-ndjson' } }),
    )
    const api = createApiClient({ fetch: fetchMock })

    const stages: string[] = []
    for await (const event of api.replan('wabdr-north', {})) stages.push(event.stage)

    expect(stages).toEqual(['route_search', 'discovery', 'done'])
  })
})

describe('error mapping', () => {
  it('exposes the stable code so callers switch on it, never on the message', async () => {
    const fetchMock = stubFetch(json({ code: 'quota_exceeded', detail: 'daily quota spent' }, 429))
    const api = createApiClient({ fetch: fetchMock })

    const error = await api
      .routeLeg({ waypoints: [{ lat: 1, lon: 2 }, { lat: 3, lon: 4 }], intent: 'unpaved' })
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('quota_exceeded')
    expect((error as ApiError).status).toBe(429)
  })

  it('survives an error body that does not follow the contract', async () => {
    // The body below is what FastAPI's built-in request-validation handler used to send: a
    // *list* under `detail` and no `code` at all, contradicting the declared ErrorResponse.
    // The backend normalises it now, so this stands as the general defence against a body
    // that did not come from the app at all — a proxy error page, or an intermediary.
    const raw = {
      detail: [
        { type: 'too_short', loc: ['body', 'waypoints'], msg: 'List should have at least 2 items' },
      ],
    }
    const fetchMock = stubFetch(json(raw, 422))
    const api = createApiClient({ fetch: fetchMock })

    const error = (await api
      .routeLeg({ waypoints: [{ lat: 1, lon: 2 }, { lat: 3, lon: 4 }], intent: 'unpaved' })
      .catch((caught: unknown) => caught)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('validation_error')
    expect(error.detail).not.toBe('')
    expect(error.body).toEqual(raw)
  })

  it('survives an error body that is not JSON at all', async () => {
    // A proxy or load balancer failing in front of the app answers with HTML.
    const fetchMock = stubFetch(
      new Response('<html><body>502 Bad Gateway</body></html>', {
        status: 502,
        headers: { 'content-type': 'text/html' },
      }),
    )
    const api = createApiClient({ fetch: fetchMock })

    const error = (await api.health().catch((caught: unknown) => caught)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(502)
    expect(error.code).toBe('unknown_error')
    expect(error.detail).not.toBe('')
  })

  it('survives a success response whose body is not JSON', async () => {
    // A misconfigured proxy can answer 200 with an HTML error page. A bare SyntaxError
    // reaching a component is unhandleable; an ApiError is not.
    const fetchMock = stubFetch(
      new Response('<html>hello</html>', { status: 200, headers: { 'content-type': 'text/html' } }),
    )
    const api = createApiClient({ fetch: fetchMock })

    const error = (await api.getTrip('wabdr-north').catch((caught: unknown) => caught)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error).not.toBeInstanceOf(SyntaxError)
    expect(error.code).toBe('unknown_error')
    expect(error.body).toBe('<html>hello</html>')
  })

  it('classifies a body that fails mid-download on the GPX path', async () => {
    // exportGpx returns a Blob rather than JSON, so it did not share readJson's guard.
    // A caller doing `if (isApiError(error))` would otherwise drop this on the floor.
    const fetchMock = stubFetch(brokenBodyResponse())
    const api = createApiClient({ fetch: fetchMock })

    const error = await api.exportGpx('wabdr-north').catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).not.toBeInstanceOf(TypeError)
  })

  it('reports a failed fetch as a network error, not as a server response', async () => {
    const cause = new TypeError('Failed to fetch')
    const fetchMock = vi.fn<FetchLike>().mockRejectedValue(cause)
    const api = createApiClient({ fetch: fetchMock })

    const error = (await api.health().catch((caught: unknown) => caught)) as ApiNetworkError

    expect(error).toBeInstanceOf(ApiNetworkError)
    expect(error.status).toBe(0)
    expect(error.cause).toBe(cause)
  })
})

describe('abort signals', () => {
  const CALLS: ReadonlyArray<readonly [string, (api: ReturnType<typeof createApiClient>, signal: AbortSignal) => unknown]> = [
    ['health', (api, signal) => api.health({ signal })],
    ['routingCapabilities', (api, signal) => api.routingCapabilities({ signal })],
    [
      'routeLeg',
      (api, signal) =>
        api.routeLeg({ waypoints: [{ lat: 1, lon: 2 }, { lat: 3, lon: 4 }], intent: 'unpaved' }, { signal }),
    ],
    ['listTrips', (api, signal) => api.listTrips({ signal })],
    ['createTrip', (api, signal) => api.createTrip({ name: 'x' }, { signal })],
    ['getTrip', (api, signal) => api.getTrip('x', { signal })],
    ['updateTrip', (api, signal) => api.updateTrip('x', { name: 'y' }, { signal })],
    ['deleteTrip', (api, signal) => api.deleteTrip('x', { signal })],
    ['exportGpx', (api, signal) => api.exportGpx('x', { signal })],
    ['placeDetail', (api, signal) => api.placeDetail('x', { signal })],
    ['replan', (api, signal) => api.replan('x', {}, { signal }).next()],
  ]

  it.each(CALLS)('%s threads the caller signal through to fetch', async (_name, invoke) => {
    const fetchMock = stubFetch(json({}))
    const api = createApiClient({ fetch: fetchMock })
    const controller = new AbortController()

    await Promise.resolve(invoke(api, controller.signal)).catch(() => undefined)

    expect(lastCall(fetchMock)[1].signal).toBe(controller.signal)
  })

  it('lets an AbortError through untouched, so DragScheduler still recognises it', async () => {
    // DragScheduler distinguishes an aborted request from a real failure with
    // `error instanceof DOMException && error.name === 'AbortError'`. Wrapping the abort
    // in an ApiError would make every superseded drag request look like an error.
    const fetchMock = vi
      .fn<FetchLike>()
      .mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const api = createApiClient({ fetch: fetchMock })

    const error = await api.health().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(DOMException)
    expect(error).not.toBeInstanceOf(ApiError)
    expect((error as DOMException).name).toBe('AbortError')
  })

  it('aborts the superseded leg request when a drag is scheduled through the client', async () => {
    // The end-to-end reason the signal has to be threaded: DragScheduler aborts the
    // in-flight preview when the user releases, and only the commit may be delivered.
    const signals: AbortSignal[] = []
    const fetchMock = vi.fn<FetchLike>().mockImplementation((_url, init) => {
      const signal = init.signal
      if (signal instanceof AbortSignal) signals.push(signal)
      return new Promise<Response>((resolve, reject) => {
        signal?.addEventListener('abort', () => {
          reject(new DOMException('The operation was aborted.', 'AbortError'))
        })
        setTimeout(() => resolve(json(LEG_RESPONSE)), 0)
      })
    })
    const api = createApiClient({ fetch: fetchMock })

    const onPreview = vi.fn()
    const onCommit = vi.fn()
    const onError = vi.fn()
    const scheduler = new DragScheduler({
      // The interval comes from the API, never from a constant in the frontend.
      intervalMs: 0,
      route: (request: Parameters<typeof api.routeLeg>[0], signal) => api.routeLeg(request, { signal }),
      onPreview,
      onCommit,
      onError,
    })

    const waypoints = [{ lat: 47.6, lon: -120.7 }, { lat: 47.7, lon: -120.6 }]
    scheduler.update({ waypoints, intent: 'unpaved' })
    scheduler.end({ waypoints, intent: 'unpaved' })
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))

    expect(signals).toHaveLength(2)
    expect(signals[0]?.aborted).toBe(true)
    expect(onPreview).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })
})

describe('typed surface', () => {
  it('accepts a leg request without the flags the backend defaults', () => {
    // Compile-time assertions: if the backend's defaults were restated as required
    // fields in the client, neither of these literals would type-check.
    const leg: Parameters<Api['routeLeg']>[0] = {
      waypoints: [
        { lat: 47.6, lon: -120.7 },
        { lat: 47.7, lon: -120.6 },
      ],
      intent: 'unpaved',
    }
    const replan: Parameters<Api['replan']>[1] = {}

    expect(leg.avoid_tolls).toBeUndefined()
    expect(replan.preserve_pinned).toBeUndefined()
  })

  it('returns the generated response types, not hand-written copies', () => {
    expectTypeOf<Api['health']>().returns.resolves.toEqualTypeOf<HealthResponse>()
    expectTypeOf<Api['getTrip']>().returns.resolves.toEqualTypeOf<Trip>()
    expectTypeOf<Api['listTrips']>().returns.resolves.toEqualTypeOf<TripSummary[]>()
    expectTypeOf<Api['routeLeg']>().returns.resolves.toEqualTypeOf<RouteLegResponse>()
    expectTypeOf<Api['routingCapabilities']>().returns.resolves.toEqualTypeOf<RoutingCapabilitiesResponse>()
    expectTypeOf<Api['placeDetail']>().returns.resolves.toEqualTypeOf<PoiDetailResponse>()
    expectTypeOf<Api['deleteTrip']>().returns.resolves.toEqualTypeOf<void>()
    expectTypeOf<Api['exportGpx']>().returns.resolves.toEqualTypeOf<Blob>()
    expectTypeOf<Api['replan']>().returns.toEqualTypeOf<AsyncGenerator<ReplanEvent, void, undefined>>()
  })
})

describe('the assistant conversation', () => {
  /**
   * Same framing as replan — newline-delimited JSON, one event per line, `done` last — so it
   * reuses the parser that is already tested against chunk boundaries mid-line, unterminated
   * final lines, and lines that are not JSON at all.
   */
  it('streams the turn event by event', async () => {
    const fetchMock = stubFetch(
      ndjsonResponse([
        '{"kind":"message","message":"Looking at your route","trip_changed":false,"truncated":false}\n',
        '{"kind":"tool_started","message":"searching","tool":"find_camps","trip_changed":false,"truncated":false}\n',
        '{"kind":"done","message":"","trip_changed":true,"truncated":false}\n',
      ]),
    )
    const api = createApiClient({ fetch: fetchMock })

    const kinds: string[] = []
    let changed = false
    for await (const event of api.chat('wabdr-north', { message: 'find me a camp' })) {
      kinds.push(event.kind)
      changed = changed || event.trip_changed
    }

    expect(kinds).toEqual(['message', 'tool_started', 'done'])
    expect(changed).toBe(true)
    const [url, init] = lastCall(fetchMock)
    expect(url).toBe('/api/trips/wabdr-north/chat')
    expect(init.method).toBe('POST')
  })

  it('sends the history the caller chose to include', async () => {
    // The server is stateless and the client owns the transcript, so what the assistant saw
    // is answerable from the request alone.
    const fetchMock = stubFetch(ndjsonResponse(['{"kind":"done","message":"","trip_changed":false,"truncated":false}\n']))
    const api = createApiClient({ fetch: fetchMock })

    await api
      .chat('wabdr-north', {
        message: 'and somewhere for fuel',
        history: [{ role: 'user', content: 'find me a camp' }],
      })
      .next()

    expect(sentJson(fetchMock)['history']).toEqual([{ role: 'user', content: 'find me a camp' }])
  })

  it('raises the typed not-implemented error while the endpoint is a stub', async () => {
    const fetchMock = stubFetch(json({ code: 'not_implemented', detail: 'chat is not implemented yet' }, 501))
    const api = createApiClient({ fetch: fetchMock })

    await expect(api.chat('x', { message: 'hello' }).next()).rejects.toBeInstanceOf(
      ApiNotImplementedError,
    )
  })

  it('threads the abort signal, so abandoning a turn stops the stream', async () => {
    const fetchMock = stubFetch(ndjsonResponse(['{"kind":"done","message":"","trip_changed":false,"truncated":false}\n']))
    const api = createApiClient({ fetch: fetchMock })
    const controller = new AbortController()

    await api.chat('x', { message: 'hello' }, { signal: controller.signal }).next()

    expect(lastCall(fetchMock)[1].signal).toBe(controller.signal)
  })
})
