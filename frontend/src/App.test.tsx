import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { ApiError, ApiNotImplementedError } from './api/errors'
import type { RequestOptions } from './api/client'
import type { GoogleMaps } from './map/loadGoogleMaps'
import {
  intentRouting,
  poi as poiFixture,
  providerCapabilities,
  routeLeg,
  routeLegResponse,
  trip as tripFixture,
  tripLeg,
  waypoint as waypointFixture,
} from './api/fixtures'
import type {
  ChatEvent,
  RouteThroughBestRequest,
  GeocodeResponse,
  ChatRequest,
  Coordinate,
  CreateTripRequest,
  ReplanEvent,
  ReplanRequest,
  Poi,
  UpdateTripRequest,
  RouteLegInput,
  RouteLegResponse,
  RoutingCapabilitiesResponse,
} from './api/types'

/**
 * The shell, and the one rule it has to prove: **chat is an accelerator, never a
 * requirement.** Placing the start and end of a trip has to work with nothing but the
 * mouse, so these tests drive the map rather than the chat rail.
 */

/**
 * The endpoints that are not built yet, answering the way the real ones do today: 501.
 *
 * Spread into every client literal, because a required method added to `AppClient` otherwise
 * breaks each of them at once — the same reason `src/api/fixtures.ts` exists for response shapes.
 * Named for the *category* rather than for `chat`, because it has now absorbed a second endpoint
 * and will absorb the next: each of these has its own behaviour tested where it lives.
 */
function stubUnbuilt() {
  return {
    routeThroughBest: vi.fn(
      (_slug: string, _request: RouteThroughBestRequest, _options?: RequestOptions) =>
        Promise.resolve({ trip: tripFixture(), added: [], left_out: [] }),
    ),
    geocode: vi.fn((_query: string, _options?: RequestOptions & { readonly near?: Coordinate }) =>
      Promise.resolve({ results: [] as GeocodeResponse['results'] }),
    ),
    exportGpx: vi.fn((_slug: string, _options?: RequestOptions) =>
      Promise.reject(new ApiNotImplementedError({ detail: 'gpx export is not implemented yet' })),
    ),
    chat: vi.fn(
      // Annotated rather than inferred: a body that only throws infers `AsyncGenerator<never>`,
      // and a test that then supplies a real event cannot type-check against it.
      // eslint-disable-next-line @typescript-eslint/require-await, require-yield
      async function* (
        _slug: string,
        _request: ChatRequest,
        _options?: RequestOptions,
      ): AsyncGenerator<ChatEvent, void, undefined> {
        throw new ApiNotImplementedError({ detail: 'chat is not implemented yet' })
      },
    ),
  }
}

/**
 * Removes the last point through the route list.
 *
 * There used to be a single "Remove last point" button. It is gone: it could only ever remove
 * the last one, which would have made the assistant the only way to take a via-point out of the
 * middle once it has `remove_waypoint`. The list can remove any of them, so these tests reach
 * the same behaviour through the row.
 */
function removeLastPoint(): void {
  const rows = within(screen.getByRole('region', { name: 'Route points' })).getAllByRole('listitem')
  const last = rows.at(-1)
  if (last === undefined) throw new Error('the route list has no points')
  fireEvent.click(within(last).getByRole('button'))
}

const PIN_PROBE_ID = 'pin-probe'

/** The shape Maps delivers position in. */
function latLngEvent(coordinate: Coordinate): unknown {
  return { latLng: { lat: () => coordinate.lat, lng: () => coordinate.lon } }
}

const CAPABILITIES: RoutingCapabilitiesResponse = {
  providers: [],
  intents: { unpaved: intentRouting({ provider: 'ors', live_update_interval_ms: 0 }) },
}

interface FakeLine {
  readonly options: Record<string, unknown>
  map: unknown
  mouseDown(coordinate: Coordinate): void
}

interface FakeMarker {
  readonly options: Record<string, unknown>
  /** Set to null when the marker is detached, which is how a removed pin disappears. */
  map: unknown
  click(): void
  contextMenu(): void
}

function createFakeMaps() {
  const maps: FakeMap[] = []
  const markers: FakeMarker[] = []
  const polylines: FakeLine[] = []
  let clickHandler: ((event: unknown) => void) | null = null

  class FakeMap {
    readonly handlers = new Map<string, (event: unknown) => void>()
    constructor() {
      maps.push(this)
    }
    addListener(event: string, handler: (event: unknown) => void): { remove: () => void } {
      if (event === 'click') clickHandler = handler
      this.handlers.set(event, handler)
      return { remove: () => this.handlers.delete(event) }
    }
    fitBounds(): void {
      // Nothing to assert here; framing is covered in MapCanvas's own tests.
    }
    setOptions(): void {
      // Panning is toggled during a drag; asserted in MapCanvas's own tests.
    }
    mouseUp(coordinate: Coordinate): void {
      this.handlers.get('mouseup')?.(latLngEvent(coordinate))
    }
    mouseMove(coordinate: Coordinate): void {
      this.handlers.get('mousemove')?.(latLngEvent(coordinate))
    }
    getZoom(): number {
      return 12
    }
  }

  const namespace = {
    Map: FakeMap,
    Polyline: class implements FakeLine {
      map: unknown
      readonly listeners = new Map<string, (event: unknown) => void>()
      constructor(readonly options: Record<string, unknown>) {
        this.map = options['map'] ?? null
        polylines.push(this)
      }
      setMap(map: unknown): void {
        this.map = map
      }
      setPath(path: unknown): void {
        this.options['path'] = path
      }
      addListener(event: string, handler: (event: unknown) => void): { remove: () => void } {
        this.listeners.set(event, handler)
        return { remove: () => this.listeners.delete(event) }
      }
      mouseDown(coordinate: Coordinate): void {
        this.listeners.get('mousedown')?.(latLngEvent(coordinate))
      }
    },
    LatLngBounds: class {
      extend(): this {
        return this
      }
    },
    marker: {
      AdvancedMarkerElement: class implements FakeMarker {
        map: unknown
        readonly listeners = new Map<string, (event: unknown) => void>()
        constructor(readonly options: Record<string, unknown>) {
          this.map = options['map'] ?? null
          markers.push(this)
        }
        addListener(event: string, handler: (event: unknown) => void): { remove: () => void } {
          this.listeners.set(event, handler)
          return { remove: () => this.listeners.delete(event) }
        }
        click(): void {
          this.listeners.get('click')?.({})
        }
        contextMenu(): void {
          this.listeners.get('contextmenu')?.({})
        }
      },
    },
  }

  return {
    loader: () => Promise.resolve(namespace as unknown as GoogleMaps),
    maps,
    markers,
    polylines,
    /** Markers whose content is a POI pin rather than a waypoint or a drag handle. */
    poiMarkers: () =>
      markers.filter(
        (marker) =>
          marker.map !== null &&
          (marker.options['content'] as HTMLElement | undefined)?.className?.startsWith('poi') ===
            true,
      ),
    clickMap(lat: number, lon: number): void {
      if (clickHandler === null) throw new Error('the map has no click listener')
      // Wrapped because the Maps API would deliver this outside React's knowledge. Without
      // act() the suite fills with warnings, which is how a real one gets missed.
      act(() => {
        clickHandler?.({ latLng: { lat: () => lat, lng: () => lon } })
      })
    },
  }
}

/**
 * Comes through the front door if it is showing, then waits for the map.
 *
 * Waiting for the map object rather than for the loading text to vanish: those are not the
 * same moment. The text goes when React commits the state change, while the map is built in a
 * passive effect that can flush a tick later — so a test that waited for the text and then
 * clicked occasionally found no click listener. Rare enough to look like a heisenbug.
 */
async function mapReady(fake: ReturnType<typeof createFakeMaps>): Promise<void> {
  const start = screen.queryByRole('button', { name: /start a new trip/i })
  if (start !== null) fireEvent.click(start)
  await waitFor(() => {
    expect(fake.maps).toHaveLength(1)
  })
}

/** The pin elements currently attached to the map. */
function attachedPins(fake: ReturnType<typeof createFakeMaps>): HTMLElement[] {
  return fake.markers
    .filter((marker) => marker.map !== null)
    .map((marker) => marker.options['content'] as HTMLElement)
}

/**
 * Waits for `expected` pins, then reads their names in route order.
 *
 * The wait and the DOM work are separate on purpose. `waitFor` retries whenever the
 * document mutates, so a polled callback that attaches nodes retriggers itself forever.
 * Only the settled result is put in the document, and into a probe node rather than
 * `document.body`, which holds the React root under test.
 */
async function pinLabels(
  fake: ReturnType<typeof createFakeMaps>,
  expected: number,
): Promise<string[]> {
  await waitFor(() => {
    expect(attachedPins(fake)).toHaveLength(expected)
  })

  const probe = document.getElementById(PIN_PROBE_ID) ?? document.createElement('div')
  probe.id = PIN_PROBE_ID
  document.body.append(probe)
  probe.replaceChildren(...attachedPins(fake))

  // Filtered by role: a pin the browser would treat as a generic div is not a pin.
  return within(probe)
    .queryAllByRole('img')
    .map((pin) => pin.title)
}

/**
 * The route a fake provider returns. Built from the factory so a field the backend adds is
 * one edit in src/api/fixtures.ts rather than a broken literal in every test file — which
 * has now happened three times, each blocking a backend handoff.
 */
const ROUTE_RESPONSE: RouteLegResponse = routeLegResponse({
  leg: routeLeg({
    geometry: [
      { lat: 47.6, lon: -120.7 },
      { lat: 47.9, lon: -120.4 },
      { lat: 48.1, lon: -120.2 },
    ],
    distance_m: 42_000,
    intent: 'twisty_paved',
  }),
  live_update_interval_ms: 0,
})

/**
 * Every call App makes, in one place.
 *
 * The same reasoning as the fixture factory: App gaining a dependency should be one edit
 * here, not a broken literal in every test that renders it. Overriding one method is
 * `{ ...fakeRouter(), routeLeg: ... }`.
 */
function fakeRouter(response: RouteLegResponse = ROUTE_RESPONSE) {
  return {
    // Annotated rather than inferred: an empty `pois: []` infers `never[]`, and a test that
    // then supplies a real place cannot type-check against it.
    // eslint-disable-next-line @typescript-eslint/require-await
    replan: vi.fn(async function* (
      _slug: string,
      _request: ReplanRequest,
      _options?: RequestOptions,
    ): AsyncGenerator<ReplanEvent, void, undefined> {
      // Nothing found, which is the honest default and today's common outcome.
      yield { stage: 'done', message: 'Done', pois: [], legs: [], progress: 1 }
    }),
    createTrip: vi.fn((request: CreateTripRequest, _options?: RequestOptions) =>
      Promise.resolve(tripFixture({ slug: request.slug ?? 'derived', name: request.name })),
    ),
    getTrip: vi.fn((slug: string, _options?: RequestOptions) =>
      Promise.resolve(tripFixture({ slug })),
    ),
    updateTrip: vi.fn((slug: string, _request: UpdateTripRequest, _options?: RequestOptions) =>
      Promise.resolve(tripFixture({ slug })),
    ),
    routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) => Promise.resolve(response)),
    routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(CAPABILITIES)),
    placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) =>
      Promise.reject(new ApiNotImplementedError({ detail: 'Places enrichment is not implemented yet' })),
    ),
      ...stubUnbuilt(),
  }
}

/**
 * The vertical slice: two clicks, one routing call, geometry on the map.
 *
 * Until this worked, nothing in the app was connected to anything else — the client, the
 * canvas and the edit helpers were four correct pieces with no join between them.
 */
describe('App routing the placed points', () => {
  it('routes two clicked points and draws what comes back', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)

    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(1))
    expect(router.routeLeg.mock.calls[0]?.[0].waypoints).toEqual([
      { lat: 47.6, lon: -120.7 },
      { lat: 48.1, lon: -120.2 },
    ])
    // The returned geometry reaches the map, not just the state.
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    expect(fake.polylines[0]?.options['path']).toEqual([
      { lat: 47.6, lng: -120.7 },
      { lat: 47.9, lng: -120.4 },
      { lat: 48.1, lng: -120.2 },
    ])
  })

  it('does not route a single point', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    await waitFor(() => expect(attachedPins(fake)).toHaveLength(1))

    expect(router.routeLeg).not.toHaveBeenCalled()
  })

  it('reports the routed distance, so the number comes from the server not the screen', async () => {
    const fake = createFakeMaps()
    render(
      <App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />,
    )
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)

    // Miles by default, so 42 km reads as 26 mi. Specific about which figure, too: the
    // surface breakdown reports distances as well, so a bare /26 mi/ matches more than one.
    expect(await screen.findByText(/points placed/)).toHaveTextContent('26 mi')
  })

  it('removes the drawn route when the points that made it are undone', async () => {
    // The state a rider reaches in the first thirty seconds: place two points, change your
    // mind. Before this, the line stayed on an empty map with a distance for a route that
    // no longer existed, and only a page reload cleared it.
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await waitFor(() => expect(fake.polylines[0]?.map).not.toBeNull())
    expect(await screen.findByText(/points placed/)).toHaveTextContent('26 mi')

    removeLastPoint()

    await waitFor(() => expect(fake.polylines.every((line) => line.map === null)).toBe(true))
    expect(screen.queryByText(/26 mi/i)).not.toBeInTheDocument()
  })

  it('drops a routing error once the points that caused it are gone', async () => {
    const fake = createFakeMaps()
    const router = {
      ...fakeRouter(),
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(new ApiError({ status: 422, code: 'no_route_found', detail: 'nope' })),
      ),
      routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(CAPABILITIES)),
      placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) =>
        Promise.reject(new ApiNotImplementedError({ detail: 'not implemented yet' })),
      ),
      ...stubUnbuilt(),
    }
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await screen.findByRole('alert')

    removeLastPoint()

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('never shows an internal error string to the rider', async () => {
    const fake = createFakeMaps()
    const router = {
      ...fakeRouter(),
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(
          new ApiError({
            status: 400,
            code: 'invalid_request',
            detail: '[fake] 51 waypoints exceeds provider maximum 50',
          }),
        ),
      ),
      routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(CAPABILITIES)),
      placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) =>
        Promise.reject(new ApiNotImplementedError({ detail: 'not implemented yet' })),
      ),
      ...stubUnbuilt(),
    }
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toContain('fake')
    expect(alert.textContent).not.toContain('provider maximum')
  })

  it('says when a route cannot be found instead of leaving the map silently empty', async () => {
    const fake = createFakeMaps()
    const router = {
      ...fakeRouter(),
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(new ApiError({ status: 422, code: 'no_route_found', detail: 'no route found' })),
      ),
      routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(CAPABILITIES)),
      placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) =>
        Promise.reject(new ApiNotImplementedError({ detail: 'not implemented yet' })),
      ),
      ...stubUnbuilt(),
    }
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)

    expect(await screen.findByRole('alert')).toHaveTextContent(/no route/i)
  })
})

/**
 * Dragging the drawn route, end to end through the real pieces.
 *
 * The map layer reports the gesture, DragSession decides where the via-point goes and
 * schedules the request, and the result comes back to the same state the canvas draws from.
 */
describe('App dragging the route', () => {
  async function routedApp() {
    const fake = createFakeMaps()
    const router = {
      ...fakeRouter(),
      routeLeg: vi.fn((request: RouteLegInput, _options?: RequestOptions) =>
        Promise.resolve({
          leg: {
            ...ROUTE_RESPONSE.leg,
            // Echo the request back, so a leg is recognisably the route that was asked for.
            geometry: [...request.waypoints],
            routed_from: { intent: request.intent, waypoints: [...request.waypoints] },
          },
          live_update_interval_ms: 0,
          estimated_duration_s: 900,
        }),
      ),
      routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(CAPABILITIES)),
      placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) =>
        Promise.reject(new ApiNotImplementedError({ detail: 'not implemented yet' })),
      ),
      ...stubUnbuilt(),
    }

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    return { fake, router }
  }

  it('survives the re-render a mid-drag preview causes', async () => {
    // The shape of a real drag: move, let a preview land, move again, release. Every other
    // drag test here fires mousedown+mouseup with nothing in between, which is why they all
    // passed while the gesture was being destroyed by its own preview. A preview sets state,
    // the re-render rebuilt the DragSession, and the release then had no gesture to end —
    // no request, no via-point, the rider's drag simply vanishing.
    const { fake, router } = await routedApp()
    const map = fake.maps[0]

    const drawnBefore = fake.polylines.length
    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.9, lon: -120.4 })
    })
    act(() => {
      map?.mouseMove({ lat: 47.9, lon: -120.6 })
    })
    // Wait for the preview to be *drawn*, not merely requested: it is the re-render it
    // causes that used to destroy the gesture, and that has not happened until the canvas
    // has redrawn.
    await waitFor(() => expect(fake.polylines.length).toBeGreaterThan(drawnBefore))
    act(() => {
      map?.mouseMove({ lat: 47.9, lon: -120.8 })
      map?.mouseUp({ lat: 47.9, lon: -121.0 })
    })

    await waitFor(() => expect(screen.getByText(/3 points placed/i)).toBeInTheDocument())
    const last = router.routeLeg.mock.calls.at(-1)?.[0]
    // The release is authoritative: the via sits where the rider let go, not where a
    // throttled preview happened to land.
    expect(last?.waypoints[1]).toEqual({ lat: 47.9, lon: -121 })
  })

  it('asks nothing of a preview-only provider until the release', async () => {
    // `live_update_interval_ms: null` is a metered engine saying "route on release, not
    // before". The moving handle is then the only mid-gesture feedback there is, which is
    // honest — but it does mean no request may go out until the rider lets go.
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.routingCapabilities.mockResolvedValue({
      providers: [],
      intents: { unpaved: intentRouting({ provider: 'ors', live_update_interval_ms: null }) },
    })
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.9, lon: -120.4 })
      map?.mouseMove({ lat: 47.9, lon: -121.0 })
    })

    // Nothing asked of the provider, and the route left exactly as it was: the moving
    // handle drawn by the canvas is the whole of the mid-gesture feedback here.
    expect(router.routeLeg).toHaveBeenCalledTimes(1)
    expect(fake.polylines).toHaveLength(1)

    // The release is still authoritative, and still routes.
    act(() => {
      map?.mouseUp({ lat: 47.9, lon: -121.0 })
    })
    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(2))
  })

  it('inserts a via-point where the line was dragged and routes through it', async () => {
    const { fake, router } = await routedApp()
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.9, lon: -120.4 }) // on the drawn line
      map?.mouseUp({ lat: 47.9, lon: -121.0 }) // dragged west
    })

    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(2))
    const request = router.routeLeg.mock.calls[1]?.[0]
    expect(request?.waypoints).toEqual([
      { lat: 47.6, lon: -120.7 },
      { lat: 47.9, lon: -121 }, // the via, between the two the rider placed
      { lat: 48.1, lon: -120.2 },
    ])
  })

  it('shows the dragged route, and does not re-request what the drag already fetched', async () => {
    // The drag routes on release. Re-routing the same waypoints afterwards would cost a
    // second request per drag against a free tier of roughly 2,000 a day.
    const { fake, router } = await routedApp()
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.9, lon: -120.4 })
      map?.mouseUp({ lat: 47.9, lon: -121.0 })
    })
    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByText(/3 points placed/i)).toBeInTheDocument())

    // Settle anything the state change might have queued.
    await act(async () => {
      await Promise.resolve()
    })
    expect(router.routeLeg).toHaveBeenCalledTimes(2)
  })
})

describe('App', () => {
  it('opens by telling the user both ways of starting', async () => {
    const fake = createFakeMaps()

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    // Behind the front door: the greeting belongs to the trip, not to the entrance.
    await mapReady(fake)

    expect(screen.getByText(/describe your trip/i)).toBeInTheDocument()
    expect(screen.getByText(/set a start and end point on the map/i)).toBeInTheDocument()
  })

  it('places the start and the end from map clicks alone', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    expect(await pinLabels(fake, 1)).toEqual(['Start'])

    fake.clickMap(48.1, -120.2)
    expect(await pinLabels(fake, 2)).toEqual(['Start', 'End'])

    fake.clickMap(48.5, -119.9)
    expect(await pinLabels(fake, 3)).toEqual(['Start', 'Via point', 'End'])
  })

  it('reports the point count, so the map is not the only feedback', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)

    expect(await screen.findByText(/1 point/i)).toBeInTheDocument()
  })

  it('keeps a one-click undo for the point just placed', async () => {
    // Route-building is click, click, click, oops: the common removal is undoing the point just
    // placed, and there is no Ctrl+Z here. Making that reflex into "read the list, find the last
    // row, hit its cross" is the difference between an undo and a task. The list is the general
    // path; this is the fast one, the same shape as right-clicking a pin.
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(48.0, -120.5)
    fake.clickMap(49.0, -121.0)
    expect(await pinLabels(fake, 3)).toHaveLength(3)

    fireEvent.click(screen.getByRole('button', { name: /remove last point/i }))

    await waitFor(() => {
      expect(screen.getByText(/2 points placed/)).toBeInTheDocument()
    })
    const rows = within(screen.getByRole('region', { name: 'Route points' })).getAllByRole('listitem')
    expect(rows.at(-1)?.textContent).toMatch(/48\.0000/)
  })

  it('offers the undo only when there is a point to undo', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    expect(screen.queryByRole('button', { name: /remove last point/i })).not.toBeInTheDocument()
  })

  it('takes a point out of the middle of the route, which only chat could do before', async () => {
    // The gap the mouse-equivalence audit found. `remove_waypoint` is one of the assistant's
    // tools, and until now the only mouse control removed the *last* point — so a via-point in
    // the middle was something only the assistant could take out.
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(48.0, -120.5)
    fake.clickMap(49.0, -121.0)
    expect(await pinLabels(fake, 3)).toHaveLength(3)

    fireEvent.click(screen.getByRole('button', { name: 'Remove point 2' }))

    await waitFor(() => {
      expect(screen.getByText(/2 points placed/)).toBeInTheDocument()
    })
    // The middle one is gone and the ends are still the ends.
    const rows = within(screen.getByRole('region', { name: 'Route points' })).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]?.textContent).toMatch(/47\.0000/)
    expect(rows[1]?.textContent).toMatch(/49\.0000/)
  })

  it('removes a point on right-click, the same idiom as adding a place', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(48.0, -120.5)
    fake.clickMap(49.0, -121.0)
    expect(await pinLabels(fake, 3)).toHaveLength(3)

    // The second route pin: markers are created in route order.
    const routePins = fake.markers.filter(
      (marker) =>
        marker.map !== null &&
        (marker.options['content'] as HTMLElement | undefined)?.className?.startsWith('pin') === true,
    )
    act(() => {
      routePins[1]?.contextMenu()
    })

    await waitFor(() => {
      expect(screen.getByText(/2 points placed/)).toBeInTheDocument()
    })
  })

  it('offers an undo for a misplaced point, and only when there is one to undo', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    expect(screen.queryByRole('region', { name: 'Route points' })).not.toBeInTheDocument()

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    expect(await pinLabels(fake, 2)).toHaveLength(2)

    removeLastPoint()

    expect(await pinLabels(fake, 1)).toEqual(['Start'])
  })
})

/**
 * Points of interest, and putting one on the route with the mouse alone.
 */
describe('App and points of interest', () => {
  const CAMP: Poi = poiFixture({
    name: 'Lone Fir Campground',
    category: 'campground',
    coordinate: { lat: 47.9, lon: -120.35 },
  })

  async function appWithPoi(place: Poi = CAMP) {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} pois={[place]} />)
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(1))
    return { fake, router }
  }

  it('adds a place to the route on right-click, routing through it', async () => {
    const { fake, router } = await appWithPoi()

    act(() => {
      fake.poiMarkers()[0]?.contextMenu()
    })

    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(2))
    const request = router.routeLeg.mock.calls[1]?.[0]
    // Inserted along the route, between the points it sits between — not appended.
    expect(request?.waypoints).toEqual([
      { lat: 47.6, lon: -120.7 },
      { lat: 47.9, lon: -120.35 },
      { lat: 48.1, lon: -120.2 },
    ])
    expect(await screen.findByText(/3 points placed/i)).toBeInTheDocument()
  })

  it('adds a place into one leg without re-routing its neighbour', async () => {
    // Found by the call count on the test above, which is not the same as being tested. The
    // handler discarded the leg structure, so a pairwise one was re-derived: adding one
    // campground split the leg it landed in, cost two requests instead of one, and re-routed
    // the segment on the far side of the trip that nobody had touched.
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(
      <App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} pois={[CAMP]} />,
    )
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    fake.clickMap(48.5, -120.0)
    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(2))

    act(() => {
      fake.poiMarkers()[0]?.contextMenu()
    })

    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(3))
    // One request, and it is the leg the place was inserted into — now three waypoints long
    // rather than two legs of two.
    expect(router.routeLeg.mock.calls[2]?.[0].waypoints).toEqual([
      { lat: 47.6, lon: -120.7 },
      { lat: 47.9, lon: -120.35 },
      { lat: 48.1, lon: -120.2 },
    ])
    // And nothing else was asked about. The far leg is untouched road.
    await new Promise((resolve) => setTimeout(resolve, 40))
    expect(router.routeLeg).toHaveBeenCalledTimes(3)
  })

  it('says why an unconfirmed suggestion cannot be added, rather than ignoring the click', async () => {
    // A disabled control with no explanation is the thing this avoids: the rider is told the
    // place has not been confirmed, which is information about the suggestion, not an error.
    const { fake, router } = await appWithPoi({ ...CAMP, source: 'llm_suggested', place_id: null })

    act(() => {
      fake.poiMarkers()[0]?.click()
    })

    expect(await screen.findByText(/not been confirmed/i)).toBeInTheDocument()
    expect(router.routeLeg).toHaveBeenCalledTimes(1)
  })
})

/**
 * Units and riding time.
 */
describe('App units and time', () => {
  async function routed() {
    const fake = createFakeMaps()
    const router = fakeRouter()
    const view = render(
      <App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />,
    )
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(1))
    return { fake, router, view }
  }

  it('shows miles by default, and switches the whole rail at once', async () => {
    // One formatter, one unit: the route summary and the surface breakdown must never
    // disagree about which system they are in.
    const { view } = await routed()
    expect(await screen.findByText(/points placed/)).toHaveTextContent('26 mi')

    fireEvent.click(screen.getByRole('button', { name: 'Kilometres' }))

    expect(await screen.findByText(/points placed/)).toHaveTextContent('42 km')
    // The breakdown followed rather than staying in miles. Scoped to the surface summary:
    // anchoring on /km$/ broke the moment a riding time was appended to the summary line, and
    // an unscoped `getByRole('list')` broke again when the route points list arrived. Neither
    // failure was about units.
    const surfaceRow = within(screen.getByRole('region', { name: /surface/i }))
      .getByRole('list')
      .querySelector('li')
    expect(surfaceRow?.textContent).toContain('km')
    expect(surfaceRow?.textContent).not.toContain('mi')
    view.unmount()
  })

  it('remembers the choice for next time', async () => {
    const first = await routed()
    fireEvent.click(screen.getByRole('button', { name: 'Kilometres' }))
    await waitFor(() => expect(screen.getByText(/points placed/)).toHaveTextContent('42 km'))
    first.view.unmount()

    await routed()

    expect(await screen.findByText(/points placed/)).toHaveTextContent('42 km')
  })

  it('says which button is the current unit, not just which looks pressed', async () => {
    await routed()

    expect(screen.getByRole('button', { name: 'Miles' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Kilometres' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('shows the riding time the routed leg came back with', async () => {
    // From the response, never from leg.duration_s — that one is a bicycle time on dirt.
    const fake = createFakeMaps()
    render(
      <App
        mapLoader={fake.loader}
        mapId="motorooter-test-vector"
        client={fakeRouter(routeLegResponse({ estimated_duration_s: 4 * 3600 + 1140 }))}
      />,
    )
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)

    // Hedged on purpose: the speeds behind it are reasoned guesses, not measurements.
    expect(await screen.findByText(/about 4h 20m/)).toBeInTheDocument()
  })

  it('shows no time until there is a route to estimate', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)

    // One point is not a route. No placeholder and not zero, which would read as "under 5m".
    await waitFor(() => expect(screen.getByText(/1 point placed/)).toBeInTheDocument())
    expect(screen.queryByText(/about/)).not.toBeInTheDocument()
  })
})

/**
 * The trip as a document.
 *
 * Before this, nothing survived a reload and nothing could be shared — and chat and replan are
 * both addressed by a slug the app never had.
 */
describe('App saving the trip', () => {
  async function placed() {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    return { fake, router }
  }

  it('creates a trip on the first point, without asking for a name', async () => {
    // A rider should not fill in a form before putting two points on a map.
    const { router } = await placed()

    await waitFor(() => expect(router.createTrip).toHaveBeenCalledTimes(1), { timeout: 3000 })
    expect(router.createTrip.mock.calls[0]?.[0].slug).toMatch(/^trip-/)
  })

  it('puts the slug in the URL and says the link is shareable', async () => {
    await placed()

    expect(await screen.findByText(/shareable/i, {}, { timeout: 3000 })).toBeInTheDocument()
    expect(new URL(window.location.href).searchParams.get('trip')).toMatch(/^trip-/)
  })

  it('saves what is on the map, not an empty document', async () => {
    const { router } = await placed()

    await waitFor(() => expect(router.updateTrip).toHaveBeenCalled(), { timeout: 3000 })
    expect(router.updateTrip.mock.calls[0]?.[1].waypoints).toHaveLength(1)
  })

  it('creates nothing for a map nobody has touched', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    // An empty map is not a trip; creating one on load would litter the bucket with blanks.
    await new Promise((resolve) => setTimeout(resolve, 1200))
    expect(router.createTrip).not.toHaveBeenCalled()
  })

  it('shows a trip named in the URL instead of an empty map', async () => {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        waypoints: [waypointFixture(47.6, -120.7), waypointFixture(48.1, -120.2)],
      }),
    )

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    // Two pins from storage, without the rider clicking anything.
    await waitFor(() => expect(attachedPins(fake)).toHaveLength(2))
  })

  it('says so when somebody else edited the trip first', async () => {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north' }))
    router.updateTrip.mockRejectedValue(
      new ApiError({ status: 409, code: 'trip_modified_concurrently', detail: 'contended' }),
    )

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)

    // Not a generic error: the rider is told their change was replaced, which is what
    // happened, and the stored trip is re-read rather than merged against.
    expect(
      await screen.findByText(/somebody else edited/i, {}, { timeout: 3000 }),
    ).toBeInTheDocument()
  })
})

/**
 * The slow path, from the button to pins on the map.
 *
 * This is the last hop between discovery and a rider seeing anything, and the states that
 * matter are the unglamorous ones: a run that takes half a minute, and a run that finds
 * nothing — which today is the common outcome.
 */
describe('App finding places', () => {
  function streaming(events: readonly ReplanEvent[], { thenHang = false } = {}) {
    return {
      ...fakeRouter(),
      replan: vi.fn(async function* (
        _slug: string,
        _request: ReplanRequest,
        _options?: RequestOptions,
      ) {
        for (const item of events) yield item
        // A run that is still going. Without this the generator returns immediately and the
        // in-progress states unmount before a test can look at them — which made one
        // assertion vacuous rather than failing.
        if (thenHang) await new Promise(() => undefined)
      }),
    }
  }

  async function routedTrip(client: ReturnType<typeof streaming>) {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={client} />)
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    // The button needs a saved trip to address.
    await waitFor(() => expect(client.createTrip).toHaveBeenCalled(), { timeout: 3000 })
    return { fake }
  }

  it('offers nothing to find until there is a route to find it along', async () => {
    const client = streaming([])
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={client} />)
    await mapReady(fake)

    expect(screen.queryByRole('button', { name: /find places/i })).not.toBeInTheDocument()
  })

  it('puts pins on the map as they are found, not when the run ends', async () => {
    const client = streaming([
      { stage: 'discovery', message: 'Searching', pois: [], legs: [], progress: 0.2 },
      {
        stage: 'discovery',
        message: 'Found one',
        pois: [poiFixture({ id: 'a', coordinate: { lat: 47.8, lon: -120.5 } })],
        legs: [],
        progress: 0.6,
      },
      { stage: 'done', message: 'Done', pois: [], legs: [], progress: 1 },
    ])
    const { fake } = await routedTrip(client)

    fireEvent.click(screen.getByRole('button', { name: /find places/i }))

    // Three pins: two waypoints and the discovered place.
    await waitFor(() => expect(attachedPins(fake)).toHaveLength(3))
  })

  it('says what it is doing while it does it', async () => {
    const client = streaming(
      [{ stage: 'discovery', message: 'Searching for camps', pois: [], legs: [], progress: 0.4 }],
      { thenHang: true },
    )
    await routedTrip(client)

    fireEvent.click(screen.getByRole('button', { name: /find places/i }))

    // A stage and a percentage, because "working" for thirty seconds reads as a hang.
    expect(await screen.findByText(/Searching for camps/)).toBeInTheDocument()
    expect(screen.getByText(/40%/)).toBeInTheDocument()
  })

  it('says it found nothing rather than leaving an empty map', async () => {
    // The common outcome today: discovery yields about two POIs from twenty-seven results.
    const client = streaming([{ stage: 'done', message: 'Done', pois: [], legs: [], progress: 1 }])
    await routedTrip(client)

    fireEvent.click(screen.getByRole('button', { name: /find places/i }))

    expect(await screen.findByText(/no places found/i)).toBeInTheDocument()
  })

  it('does not run twice at once', async () => {
    const client = streaming(
      [{ stage: 'discovery', message: 'Searching', pois: [], legs: [], progress: 0.1 }],
      { thenHang: true },
    )
    await routedTrip(client)

    fireEvent.click(screen.getByRole('button', { name: /find places/i }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /finding places/i })).toBeDisabled(),
    )
  })

  it('keeps the map usable while the slow path runs', async () => {
    // The two speeds must never block each other: dragging during a replan has to work.
    const client = streaming(
      [{ stage: 'discovery', message: 'Searching', pois: [], legs: [], progress: 0.1 }],
      { thenHang: true },
    )
    const { fake } = await routedTrip(client)
    await waitFor(() => expect(fake.polylines.length).toBeGreaterThan(0))

    fireEvent.click(screen.getByRole('button', { name: /find places/i }))
    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.9, lon: -120.4 })
    })

    // The gesture started, mid-replan.
    expect(client.routeLeg).toHaveBeenCalled()
    act(() => {
      fake.maps[0]?.mouseUp({ lat: 47.9, lon: -121 })
    })
    await waitFor(() => expect(client.routeLeg).toHaveBeenCalledTimes(2))
  })
})

/**
 * The front door.
 *
 * The landing screen reverses the earlier "no dialog" decision for arrivals only: a rider
 * coming cold needs somewhere to start, and click-to-create still works once on the map.
 */
describe('App arriving', () => {
  it('shows the front door rather than an empty map', () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)

    expect(screen.getByRole('button', { name: /start a new trip/i })).toBeInTheDocument()
    expect(screen.queryByLabelText('Route map')).not.toBeInTheDocument()
  })

  it('carries the name from the door into the trip it creates', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)

    fireEvent.change(screen.getByRole('textbox', { name: /trip name/i }), {
      target: { value: 'Cascades loop' },
    })
    fireEvent.click(screen.getByRole('button', { name: /start a new trip/i }))
    await waitFor(() => expect(fake.maps).toHaveLength(1))
    fake.clickMap(47.6, -120.7)

    await waitFor(() => expect(router.createTrip).toHaveBeenCalled(), { timeout: 3000 })
    expect(router.createTrip.mock.calls[0]?.[0].name).toBe('Cascades loop')
  })

  it('shows and remembers the name it created the trip with', async () => {
    // The test that was missing. The existing one asserts on createTrip's *request*, which was
    // correct and passing while everything downstream fell back to "Untitled trip": a trip
    // created this session is never re-read, so the stored document stayed null all session
    // and both the heading and the list lost the rider's own name.
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)

    fireEvent.change(screen.getByRole('textbox', { name: /trip name/i }), {
      target: { value: 'Cascades loop' },
    })
    fireEvent.click(screen.getByRole('button', { name: /start a new trip/i }))
    await waitFor(() => expect(fake.maps).toHaveLength(1))
    fake.clickMap(47.6, -120.7)

    // On screen…
    expect(await screen.findByRole('heading', { name: 'Cascades loop' })).toBeInTheDocument()
    // …and in the list they will come back to.
    await waitFor(() => {
      expect(localStorage.getItem('motorooter.visitedTrips')).toContain('Cascades loop')
    })
  })

  it('starts without a name rather than making the rider invent one', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)

    fireEvent.click(screen.getByRole('button', { name: /start a new trip/i }))
    await waitFor(() => expect(fake.maps).toHaveLength(1))
    fake.clickMap(47.6, -120.7)

    await waitFor(() => expect(router.createTrip).toHaveBeenCalled(), { timeout: 3000 })
    expect(router.createTrip.mock.calls[0]?.[0].name).toBe('Untitled trip')
  })

  it('goes straight to the map when the link already names a trip', async () => {
    // A shared link must not stop at a door the recipient did not ask for.
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'WABDR North' }))

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)

    await waitFor(() => expect(fake.maps).toHaveLength(1))
    expect(screen.queryByRole('button', { name: /start a new trip/i })).not.toBeInTheDocument()
  })

  it('takes you back to a trip it has seen before, next time', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'WABDR North' }))
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const first = render(
      <App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />,
    )
    // Wait for the *write*, not for the name to appear. The rail shows the name as soon as the
    // trip loads, but the list is recorded in a later effect — so waiting on the heading let
    // the unmount beat the write, and the second render found an empty list. Roughly one run
    // in four, which findBy on the far side could not fix because nothing was ever stored.
    await waitFor(() => {
      expect(localStorage.getItem('motorooter.visitedTrips')).toContain('wabdr-north')
    })
    first.unmount()
    router.getTrip.mockClear()

    // A fresh arrival with no trip in the URL. This used to assert the door listed the trip;
    // auto-select means the trip opens itself instead, which proves the same thing more
    // strongly — the visit was recorded, and it is what got the rider back here.
    window.history.replaceState(null, '', '/')
    const second = createFakeMaps()
    render(<App mapLoader={second.loader} mapId="motorooter-test-vector" client={router} />)

    await waitFor(() => {
      expect(router.getTrip).toHaveBeenCalledWith('wabdr-north', expect.anything())
    })
  })
})

describe('a trip the assistant built', () => {
  /**
   * The join nobody had exercised.
   *
   * A chat turn builds a trip server-side and writes legs with `routed: null` — routing happens
   * on this side, so the document arrives with structure and no geometry. That is correct, and it
   * means the headline feature only works if the client completes it. The integrator read a real
   * six-point loop back from chat and got five legs and 0.0 mi, which is expected but had never
   * been checked from the browser end.
   *
   * If this were broken, Tim would type his trip, see six pins and no line, and conclude the
   * whole thing was.
   */
  function unrouted(count: number) {
    const waypoints = Array.from({ length: count + 1 }, (_u, i) => waypointFixture(47 + i * 0.2, -120 - i * 0.2))
    return {
      waypoints,
      legs: Array.from({ length: count }, (_u, i) =>
        tripLeg({ start_waypoint_index: i, end_waypoint_index: i + 1, routed: null }),
      ),
    }
  }

  it('routes and draws a trip that arrives with structure and no geometry', async () => {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    const { waypoints, legs } = unrouted(5)
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'Loop', waypoints, legs }))
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    // One request per leg, and a line on the map for each.
    await waitFor(() => {
      expect(router.routeLeg).toHaveBeenCalledTimes(5)
    })
    await waitFor(() => {
      expect(fake.polylines.filter((line) => line.map !== null).length).toBeGreaterThanOrEqual(5)
    })
  })

  it('asks for every leg at once rather than one after another', async () => {
    // Six legs at ~900 ms each is the difference between a blink and a five-second wait, and it
    // is the whole of what a rider experiences between "pins appear" and "route appears".
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    let inFlight = 0
    let peak = 0
    router.routeLeg.mockImplementation(
      () =>
        new Promise((resolve) => {
          inFlight += 1
          peak = Math.max(peak, inFlight)
          setTimeout(() => {
            inFlight -= 1
            resolve(ROUTE_RESPONSE)
          }, 20)
        }),
    )
    const { waypoints, legs } = unrouted(5)
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'Loop', waypoints, legs }))
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    await waitFor(() => {
      expect(router.routeLeg).toHaveBeenCalledTimes(5)
    })
    // Sequential would peak at one. Wall clock is then one leg, not five.
    expect(peak).toBe(5)
  })

  it('routes a trip the assistant builds mid-session, which is the real path', async () => {
    // `trip_changed` tells the rail to re-read rather than reconstruct, and the re-read brings
    // legs with no geometry. Nothing routes them unless the same path that handles a loaded trip
    // also handles a replaced one.
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'Loop', waypoints: [], legs: [] }))
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    await waitFor(() => expect(router.getTrip).toHaveBeenCalled())
    expect(router.routeLeg).not.toHaveBeenCalled()

    // The assistant builds the trip and says so.
    const { waypoints, legs } = unrouted(3)
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'Loop', waypoints, legs }))
    router.chat.mockImplementation(
      // eslint-disable-next-line @typescript-eslint/require-await
      async function* () {
        yield { kind: 'done' as const, message: '', tool: null, trip_changed: true, truncated: false, progress: null }
      },
    )
    fireEvent.change(screen.getByRole('textbox', { name: /ask the assistant/i }), {
      target: { value: 'three days of dirt out of Woodinville' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => {
      expect(router.routeLeg).toHaveBeenCalledTimes(3)
    })
    await waitFor(() => {
      expect(screen.getByText(/4 points placed/)).toBeInTheDocument()
    })
  })
})

describe('adding a point by typing its name', () => {
  /**
   * The half of the original trip-creation spec that never shipped — "type a starting and ending
   * address or choose to click on the map" — because geocoding did not exist until now.
   */
  async function ready() {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    return { fake, router }
  }

  async function look(text: string): Promise<void> {
    fireEvent.change(screen.getByRole('searchbox', { name: /add a place by name/i }), {
      target: { value: text },
    })
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }))
    await Promise.resolve()
  }

  it('puts the chosen place on the route, named rather than as a coordinate', async () => {
    // The point of typing a name: the route reads back as places instead of numbers, which is
    // the last thing in the app that made a rider read coordinates.
    const { fake, router } = await ready()
    router.geocode.mockResolvedValue({
      results: [
        {
          name: 'Leavenworth',
          place_id: 'ChIJ1',
          coordinate: { lat: 47.5962, lon: -120.6615 },
          address: 'Leavenworth, WA 98826, USA',
          kinds: ['locality'],
        },
      ],
    })

    await look('leavenworth')
    fireEvent.click(await screen.findByRole('button', { name: /Leavenworth, WA 98826/ }))

    await waitFor(() => {
      expect(screen.getByText(/1 point placed/)).toBeInTheDocument()
    })
    const rows = within(screen.getByRole('region', { name: 'Route points' })).getAllByRole(
      'listitem',
    )
    expect(rows[0]?.textContent ?? '').toMatch(/Leavenworth/)
    expect(fake.markers.filter((marker) => marker.map !== null).length).toBeGreaterThan(0)
  })

  it('biases the search toward the trip once it has a point', async () => {
    // What makes "Leavenworth" the Washington one. Sent only when there is somewhere to bias
    // toward — a made-up centre would silently prefer one real place over another.
    const { fake, router } = await ready()
    router.geocode.mockResolvedValue({ results: [] })

    await look('leavenworth')
    await waitFor(() => expect(router.geocode).toHaveBeenCalled())
    expect(router.geocode.mock.calls[0]?.[1]?.near).toBeUndefined()

    fake.clickMap(47.75, -122.16)
    await look('leavenworth')

    await waitFor(() => expect(router.geocode).toHaveBeenCalledTimes(2))
    expect(router.geocode.mock.calls[1]?.[1]?.near).toEqual({ lat: 47.75, lon: -122.16 })
  })
})

describe('a trip that comes back to where it started', () => {
  it('handles a route returning to its first point', async () => {
    // The shape of the first trip anyone actually planned: "starting in Woodinville, WA going
    // east and coming back". Coming back repeats a coordinate, which the route list keyed on.
    const warned: unknown[][] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      warned.push(args)
    })
    try {
      const fake = createFakeMaps()
      const router = fakeRouter()
      render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
      await mapReady(fake)

      fake.clickMap(47.75, -122.16)
      fake.clickMap(47.52, -120.46)
      fake.clickMap(47.75, -122.16)

      await waitFor(() => {
        expect(screen.getByText(/3 points placed/)).toBeInTheDocument()
      })
      const rows = within(screen.getByRole('region', { name: 'Route points' })).getAllByRole(
        'listitem',
      )
      expect(rows).toHaveLength(3)
      expect(warned.flat().join(' ')).not.toMatch(/same key|duplicate key/i)
    } finally {
      spy.mockRestore()
    }
  })

  it('removes the return leg rather than the outbound one', async () => {
    // Two stops at one place are different stops. Getting this wrong deletes the start of the
    // trip when a rider meant to drop the finish.
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)
    fake.clickMap(47.75, -122.16)
    fake.clickMap(47.52, -120.46)
    fake.clickMap(47.75, -122.16)
    await waitFor(() => {
      expect(screen.getByText(/3 points placed/)).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Remove point 3' }))

    await waitFor(() => {
      expect(screen.getByText(/2 points placed/)).toBeInTheDocument()
    })
    const rows = within(screen.getByRole('region', { name: 'Route points' })).getAllByRole(
      'listitem',
    )
    // The one that survives is the start, not the finish.
    expect(rows[0]?.textContent ?? '').toMatch(/47\.7500/)
    expect(rows[1]?.textContent ?? '').toMatch(/47\.5200/)
  })
})

describe('where the place details go', () => {
  /**
   * Tim, after planning a real trip: *"I don't love that the details go in the bottom of the right
   * hand pane... I'd like that view to be separate."*
   *
   * "Separate" is the request, and the thing to assert is containment rather than appearance —
   * being at the bottom of the rail is precisely what he objected to, and it is exactly what a
   * test can see.
   */
  async function opened() {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        name: 'WABDR North',
        waypoints: [waypointFixture(47, -120), waypointFixture(48, -120)],
        pois: [poiFixture({ id: 'a', name: 'Lone Fir', source: 'places', place_id: 'p1' })],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fireEvent.click(await screen.findByRole('button', { name: /^Lone Fir/ }))
    return { fake, router, pane: await screen.findByRole('complementary', { name: 'Lone Fir' }) }
  }

  it('puts the details outside the rail, not at the bottom of it', async () => {
    const { pane } = await opened()

    const rail = screen.getByRole('complementary', { name: 'Trip assistant' })
    expect(rail.contains(pane)).toBe(false)
  })

  it('leaves the map and the rail usable while it is open', async () => {
    // The reason it stopped being a modal. A rider looking at a place should be able to click the
    // map and scroll the rail at the same time — that is what "separate" buys over a dialog.
    const { pane } = await opened()

    expect(pane).not.toHaveAttribute('aria-modal')
    expect(screen.getByRole('main', { name: 'Route map' })).not.toHaveAttribute('inert')
    expect(screen.getByRole('button', { name: /find places/i })).toBeEnabled()
  })

  it('dismisses on the X and gives the space back', async () => {
    await opened()

    fireEvent.click(screen.getByRole('button', { name: /close place details/i }))

    await waitFor(() => {
      expect(screen.queryByRole('complementary', { name: 'Lone Fir' })).not.toBeInTheDocument()
    })
  })

  it('opens the same pane from a map pin as from the list', async () => {
    // Both entry points, still one view.
    const { fake } = await opened()
    fireEvent.click(screen.getByRole('button', { name: /close place details/i }))
    await waitFor(() => {
      expect(screen.queryByRole('complementary', { name: 'Lone Fir' })).not.toBeInTheDocument()
    })

    act(() => {
      fake.poiMarkers()[0]?.click()
    })

    expect(await screen.findByRole('complementary', { name: 'Lone Fir' })).toBeInTheDocument()
  })
})

describe('how much the trip climbs', () => {
  /**
   * Suppressed for months on a real discrepancy, now explained: ORS returned an exact 0 where its
   * elevation lookup failed, and twelve such points in 2,763 accounted for 3,124 m. With that
   * filtered the figure is worth showing — a 3,600 m day and a 1,500 m day are different rides
   * over the same distance.
   *
   * The care needed is that Google reports no elevation at all, so on a mixed trip the figure
   * covers only part of the route. Unknown stays unknown, exactly as it does for surface.
   */
  function withLegs(ascents: readonly (number | null)[]) {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    const waypoints = ascents.map((_a, index) => waypointFixture(47 + index * 0.5, -120))
    waypoints.push(waypointFixture(47 + ascents.length * 0.5, -120))
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        name: 'WABDR North',
        waypoints,
        legs: ascents.map((ascent, index) =>
          tripLeg({
            start_waypoint_index: index,
            end_waypoint_index: index + 1,
            routed: routeLeg({
              ascent_m: ascent,
              distance_m: 40_000,
              routed_from: {
                intent: 'unpaved',
                waypoints: [
                  waypoints[index]?.coordinate ?? { lat: 0, lon: 0 },
                  waypoints[index + 1]?.coordinate ?? { lat: 0, lon: 0 },
                ],
              },
            }),
          }),
        ),
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    return { fake, router }
  }

  it('shows the climb when every leg measured it', async () => {
    const { fake } = withLegs([800, 700])
    await mapReady(fake)
    // Miles is the default, so metres need the toggle — which also exercises the unit path
    // rather than asserting on whichever unit happens to be default.
    fireEvent.click(screen.getByRole('button', { name: 'Kilometres' }))

    expect(await screen.findByText(/1,500 m/)).toBeInTheDocument()
  })

  it('says how far went unmeasured rather than passing it off as flat', async () => {
    // The mixed-trip case. Google reports no elevation, so a figure covering 40 km of 120 must
    // not read as the whole trip's climb — it would understate it threefold.
    const { fake } = withLegs([1200, null, null])
    await mapReady(fake)
    fireEvent.click(screen.getByRole('button', { name: 'Kilometres' }))

    // Scoped to the trip total: each segment now shows its own climb, so an unscoped matcher
    // finds the leg figure as well and stops being about the trip.
    await waitFor(() => {
      expect(document.querySelector('.route-summary__climb')?.textContent ?? '').toMatch(/1,200 m/)
    })
    const climb = document.querySelector('.route-summary__climb')?.textContent ?? ''
    expect(climb).toMatch(/unmeasured|not measured/i)
    expect(climb).toMatch(/80 km/)
  })

  it('says nothing at all when no engine measured any of it', async () => {
    // Zero would be a claim about a route nobody has measured, and a trip routed entirely through
    // Google is the common case for Fast and Twisties.
    const { fake } = withLegs([null, null])
    await mapReady(fake)
    await waitFor(() => {
      expect(screen.getByText(/points placed/)).toBeInTheDocument()
    })

    expect(document.querySelector('.route-summary__climb')).toBeNull()
  })

  it('reads in feet for a rider in miles, not converted as a distance', async () => {
    // 3,600 m is 11,800 ft. Converting climb by 1609 would show "2.2" — a plausible-looking
    // small number rather than an obvious error.
    const { fake } = withLegs([3600])
    await mapReady(fake)

    await waitFor(() => {
      expect(document.querySelector('.route-summary__climb')?.textContent ?? '').toMatch(/11,800 ft/)
    })
  })
})

describe('where the riding time came from', () => {
  /**
   * The field exists because backend argued for it in these terms: a rider must not get a number
   * that looks exact when half of it is a guess.
   *
   * `formatDuration` already says "about", but that is about rounding to five minutes — it says
   * nothing about whether the figure is an engine's measurement or our speed table. Those are
   * different claims and only one of them was being made.
   */
  function loaded(estimated: boolean) {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.routeLeg.mockImplementation((request: RouteLegInput) =>
      Promise.resolve(
        routeLegResponse({
          leg: routeLeg({
            geometry: [...request.waypoints],
            distance_m: 40_000,
            duration_is_trustworthy: !estimated,
          }),
          estimated_duration_s: 3600,
        }),
      ),
    )
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        name: 'WABDR North',
        waypoints: [waypointFixture(47, -120), waypointFixture(48, -120)],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    return { fake, router }
  }

  /** The caveat attached to the trip total, not the one on a segment's mode picker. */
  function summaryNote(): HTMLElement | null {
    return document.querySelector('.route-summary__provenance')
  }

  it('says when part of the time is our own model', async () => {
    const { fake } = loaded(true)
    await mapReady(fake)

    await waitFor(() => {
      expect(summaryNote()).not.toBeNull()
    })
    expect(summaryNote()?.textContent ?? '').toMatch(/our estimate, not the engine/i)
  })

  it('says nothing when the figure is the engine own measurement', async () => {
    const { fake } = loaded(false)
    await mapReady(fake)
    await waitFor(() => {
      expect(screen.getByText(/points placed/)).toHaveTextContent(/about/)
    })

    // The absence is the signal. A trip whose every leg was measured needs no caveat, and
    // labelling it "measured" would put ink on the ordinary case.
    expect(summaryNote()).toBeNull()
  })

  it('does not call the estimate unreliable, because on dirt it is the better number', async () => {
    // Hosted ORS reported 143 min for a 40 km leg that takes about 46. A rider must not come away
    // believing the dirt figure is the dodgy one.
    const { fake } = loaded(true)
    await mapReady(fake)
    await waitFor(() => {
      expect(summaryNote()).not.toBeNull()
    })

    expect(summaryNote()?.textContent ?? '').not.toMatch(
      /unreliable|inaccurate|cannot be trusted|rough guess/i,
    )
  })
})

describe('what the rail puts first', () => {
  /**
   * The invariant this exists to protect, and the one that regressed twice.
   *
   * Tim failed to find two on-screen controls, and both times the answer was "scroll down". The
   * cause was structural: the button that *produces* the places sat below the list of places it
   * produced, so the better it worked the further away it moved. Measured in Chrome at five
   * points and thirty places, the rail was 3017 px and the primary action was 2477 px down.
   *
   * jsdom has no layout, so height is not assertable here — but document order is, and order is
   * what the fix rests on. Inputs above outputs: a reorder that undid it would pass every other
   * test in this file.
   */
  function railOrder(): string[] {
    const rail = screen.getByRole('complementary', { name: 'Trip assistant' })
    const marks: { at: number; name: string }[] = []
    const note = (name: string, node: Element | null) => {
      if (node !== null) marks.push({ at: [...rail.querySelectorAll('*')].indexOf(node), name })
    }
    note('replan', rail.querySelector('.replan'))
    note('gpx', rail.querySelector('.gpx'))
    note('chat', rail.querySelector('.chat'))
    note('points', rail.querySelector('.points'))
    note('places', rail.querySelector('.places'))
    return marks.sort((a, b) => a.at - b.at).map((mark) => mark.name)
  }

  it('puts the actions above the output they produce', async () => {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        name: 'WABDR North',
        waypoints: [waypointFixture(47, -120), waypointFixture(48, -120), waypointFixture(49, -120)],
        pois: [
          poiFixture({ id: 'a', name: 'Lone Fir', source: 'places', place_id: 'p1' }),
          poiFixture({ id: 'b', name: 'Chevron', category: 'fuel', source: 'places', place_id: 'p2' }),
        ],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    await screen.findByRole('button', { name: /^Lone Fir/ })

    // Replan and GPX are asks; chat is an ask; points and places are what came back.
    expect(railOrder()).toEqual(['replan', 'gpx', 'chat', 'points', 'places'])
  })

  it('keeps the primary action ahead of the points list however long it gets', async () => {
    // The specific way it went wrong: the points list grows with every point placed, so an
    // action below it recedes as the trip gets longer — exactly backwards, since a longer route
    // is more worth searching.
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)
    for (const lat of [47.0, 47.5, 48.0, 48.5, 49.0]) fake.clickMap(lat, -120)
    await waitFor(() => {
      expect(screen.getByText(/5 points placed/)).toBeInTheDocument()
    })

    const order = railOrder()
    expect(order.indexOf('replan')).toBeLessThan(order.indexOf('points'))
  })
})

describe('exporting the trip for a GPS unit', () => {
  it('cannot export a trip with nowhere to go', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)

    expect(screen.getByRole('button', { name: /download gpx/i })).toBeDisabled()
  })

  it('says what travels, including the places found along the way', async () => {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        name: 'WABDR North',
        waypoints: [waypointFixture(47, -120), waypointFixture(48, -120)],
        pois: [
          poiFixture({ id: 'a', name: 'Lone Fir', source: 'places', place_id: 'p1' }),
          poiFixture({ id: 'b', name: 'Chevron', category: 'fuel', source: 'places', place_id: 'p2' }),
        ],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    expect(await screen.findByText(/2 places as waypoints/i)).toBeInTheDocument()
  })

  it('leaves an ignored place out of the file, not just off the map', async () => {
    // The export reads the stored document, and ignoring removes the place from it. So this is
    // the same decision surfacing in a second place rather than a second mechanism.
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        name: 'WABDR North',
        waypoints: [waypointFixture(47, -120), waypointFixture(48, -120)],
        pois: [
          poiFixture({ id: 'a', name: 'Lone Fir', source: 'places', place_id: 'p1' }),
          poiFixture({ id: 'b', name: 'Chevron', category: 'fuel', source: 'places', place_id: 'p2' }),
        ],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    await screen.findByText(/2 places as waypoints/i)

    fireEvent.click(screen.getByRole('button', { name: 'Ignore Lone Fir' }))

    expect(await screen.findByText(/1 places as waypoints/i)).toBeInTheDocument()
  })

  it('reads as not built yet rather than broken, because it is still a stub', async () => {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        name: 'WABDR North',
        waypoints: [waypointFixture(47, -120), waypointFixture(48, -120)],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fireEvent.click(await screen.findByRole('button', { name: /download gpx/i }))

    expect(await screen.findByText(/export is not built yet/i)).toBeInTheDocument()
  })
})

describe('choosing what discovery looks for', () => {
  it('sends only the kinds the rider chose', async () => {
    // The last mouse-equivalence gap: `find_places` takes categories and the mouse could only
    // say "everything", so "find me more restaurants" worked by typing and not by clicking.
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(48.0, -120.5)
    await waitFor(() => expect(router.createTrip).toHaveBeenCalled(), { timeout: 3000 })

    fireEvent.click(screen.getByRole('checkbox', { name: 'Food' }))
    fireEvent.click(screen.getByRole('button', { name: /find places/i }))

    await waitFor(() => expect(router.replan).toHaveBeenCalled())
    const sent = router.replan.mock.calls[0]?.[1].categories ?? []
    expect(sent).toContain('food')
    // Off by default on purpose: a fuel station every 25 km is not information, and it is one
    // of the most expensive things to search for.
    expect(sent).not.toContain('fuel')
  })

  it('says what the narrowing buys, because otherwise there is no reason to do it', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(48.0, -120.5)

    expect(await screen.findByText(/5 of 9 kinds/)).toBeInTheDocument()
    expect(screen.getByText(/fewer searches/i)).toBeInTheDocument()
  })
})

describe('deciding about the places discovery found', () => {
  /** A trip already carrying discovered places, which is what a replan leaves behind. */
  function withPlaces() {
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'wabdr-north',
        name: 'WABDR North',
        waypoints: [waypointFixture(47, -120), waypointFixture(48, -120)],
        // A routed leg, because a place can only be inserted along geometry that exists —
        // `nearestLeg` has nothing to measure against otherwise.
        legs: [
          tripLeg({
            start_waypoint_index: 0,
            end_waypoint_index: 1,
            routed: routeLeg({
              geometry: [
                { lat: 47, lon: -120 },
                { lat: 47.5, lon: -120 },
                { lat: 48, lon: -120 },
              ],
            }),
          }),
        ],
        pois: [
          poiFixture({
            id: 'a',
            name: 'Lone Fir',
            category: 'campground',
            source: 'places',
            place_id: 'p1',
            coordinate: { lat: 47.5, lon: -120 },
          }),
          poiFixture({
            id: 'b',
            name: 'Chevron',
            category: 'fuel',
            source: 'places',
            place_id: 'p2',
            coordinate: { lat: 47.7, lon: -120 },
          }),
        ],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    return { fake, router }
  }

  it('routes through a whole group in one action, in route order', async () => {
    // The bulk half of item 5, which has never existed. Per group rather than one button for
    // everything found: twenty-nine places is a search result, not an itinerary.
    const { fake, router } = withPlaces()
    await mapReady(fake)
    await screen.findByRole('button', { name: /^Lone Fir/ })
    router.routeLeg.mockClear()

    fireEvent.click(screen.getByRole('button', { name: /route through 1 stay/i }))

    await waitFor(() => expect(router.routeLeg).toHaveBeenCalled())
    // One press, one leg re-requested — not one request per place.
    expect(router.routeLeg).toHaveBeenCalledTimes(1)
    const request = router.routeLeg.mock.calls[0]?.[0]
    expect(request?.waypoints.map((point) => point.lat)).toContain(47.5)
  })

  it('lists them in the rail, because pins alone were not findable', async () => {
    // Tim, after a run that found twenty-nine places: "I don't see any to click on". They were
    // pins. He was right anyway — the rail is where a rider decides.
    const { fake } = withPlaces()
    await mapReady(fake)

    expect(await screen.findByRole('button', { name: /^Lone Fir/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Chevron/ })).toBeInTheDocument()
  })

  it('opens the same pane from a list row as from a pin', async () => {
    const { fake } = withPlaces()
    await mapReady(fake)

    fireEvent.click(await screen.findByRole('button', { name: /^Lone Fir/ }))

    expect(await screen.findByRole('complementary', { name: 'Lone Fir' })).toBeInTheDocument()
  })

  it('takes an ignored place off the trip, not just off the screen', async () => {
    // Discovered places are persisted — `placed` feeds the save — so hiding one without
    // removing it means it comes straight back on the next load.
    const { fake, router } = withPlaces()
    await mapReady(fake)
    await screen.findByRole('button', { name: /^Lone Fir/ })

    fireEvent.click(screen.getByRole('button', { name: 'Ignore Lone Fir' }))

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /^Lone Fir/ })).not.toBeInTheDocument()
    })
    await waitFor(() => expect(router.updateTrip).toHaveBeenCalled(), { timeout: 3000 })
    const saved = router.updateTrip.mock.calls.at(-1)?.[1].pois ?? []
    expect(saved.map((place) => place.id)).toEqual(['b'])
  })

  it('lets the rider put back a place they ignored by mistake', async () => {
    // One click removes something a two-minute discovery run found, so a mis-click needs a way
    // back that is not "run discovery again".
    const { fake } = withPlaces()
    await mapReady(fake)
    await screen.findByRole('button', { name: /^Lone Fir/ })

    fireEvent.click(screen.getByRole('button', { name: 'Ignore Lone Fir' }))
    fireEvent.click(await screen.findByRole('button', { name: /undo/i }))

    expect(await screen.findByRole('button', { name: /^Lone Fir/ })).toBeInTheDocument()
  })

  it('keeps the places already on the trip when a replan finds different ones', async () => {
    // The test that should have existed all along. `preserve_pinned` was declared, defaulted to
    // true, and sent by this client — and the handler never read it. The rider's places survive
    // because *this side* unions the stream into what the trip already holds, which is the only
    // place it can happen: replan streams and never writes the trip. The behaviour was right for
    // a reason nobody had written down, and it was asserted nowhere.
    const { fake, router } = withPlaces()
    await mapReady(fake)
    await screen.findByRole('button', { name: /^Lone Fir/ })

    router.replan.mockImplementation(
      // eslint-disable-next-line @typescript-eslint/require-await
      async function* () {
        yield {
          stage: 'discovery',
          message: 'Found places',
          progress: 1,
          pois: [
            poiFixture({
              id: 'new',
              name: 'Halfway Flat',
              category: 'wild_camp',
              source: 'places',
              place_id: 'p9',
            }),
          ],
          legs: [],
        }
      },
    )
    fireEvent.click(screen.getByRole('button', { name: /find places/i }))

    // The new one arrives and the rider's own two are still there.
    expect(await screen.findByRole('button', { name: /^Halfway Flat/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Lone Fir/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Chevron/ })).toBeInTheDocument()
  })

  it('keeps an ignored place off the list when a replan finds it again', async () => {
    // The stream unions into what is shown, so an ignore that only filtered the document would
    // be undone by the next run turning the same place up.
    const { fake, router } = withPlaces()
    await mapReady(fake)
    await screen.findByRole('button', { name: /^Lone Fir/ })
    fireEvent.click(screen.getByRole('button', { name: 'Ignore Lone Fir' }))
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /^Lone Fir/ })).not.toBeInTheDocument()
    })

    router.replan.mockImplementation(
      // eslint-disable-next-line @typescript-eslint/require-await
      async function* () {
        yield {
          stage: 'discovery',
          message: 'Found places',
          progress: 1,
          pois: [
            poiFixture({ id: 'a', name: 'Lone Fir', category: 'campground', source: 'places', place_id: 'p1' }),
          ],
          legs: [],
        }
      },
    )
    fireEvent.click(screen.getByRole('button', { name: /find places/i }))

    await waitFor(() => expect(router.replan).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /^Lone Fir/ })).not.toBeInTheDocument()
  })
})

describe('choosing how a segment routes', () => {
  it('changes one segment and re-routes only that one', async () => {
    // The second mouse-equivalence gap. `set_leg_intent` is one of the assistant's tools and
    // there was no per-leg control at all, so chat would have been the only way to change a
    // routing mode. `TripLeg.intent` has been per-leg in the data since the first backend
    // branch — it was inert while a trip was one leg spanning everything.
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(48.0, -120.5)
    fake.clickMap(49.0, -121.0)
    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(2))
    router.routeLeg.mockClear()

    const pickers = screen.getAllByRole('combobox')
    fireEvent.change(pickers[0] as HTMLElement, { target: { value: 'highway_connector' } })

    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(1))
    const request = router.routeLeg.mock.calls[0]?.[0]
    expect(request?.intent).toBe('highway_connector')
    // The first segment's own waypoints. The second segment is untouched road.
    expect(request?.waypoints).toEqual([
      { lat: 47, lon: -120 },
      { lat: 48, lon: -120.5 },
    ])
  })

  it('tells the rider a mode costs the surface breakdown, from the API rather than a list', async () => {
    // Measured live: Google returns zero spans, so 229 of 269 km of a real trip rendered grey.
    // The hardcoded version of this went stale the day the policy table repointed an intent.
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.routingCapabilities.mockResolvedValue({
      providers: [
        providerCapabilities({
          name: 'google',
          alternatives: true,
          max_waypoints: 25,
          live_update_interval_ms: 1000,
        }),
      ],
      intents: {
        highway_connector: intentRouting({ provider: 'google', live_update_interval_ms: 1000 }),
      },
    })
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(48.0, -120.5)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'highway_connector' } })

    // One note covering both, because Google reports neither surface nor elevation — the real
    // capability table says so, which is why this reads as one cause rather than two problems.
    expect(await screen.findByText(/no surface or climb data/i)).toBeInTheDocument()
  })
})

describe('dragging one leg of a multi-leg trip', () => {
  /**
   * A trip loaded with two legs on different intents.
   *
   * Loaded rather than clicked, because there is no mode picker yet — that is the next branch.
   * The legs still carry real intents, which is what the drag has to respect.
   */
  function mixedTrip() {
    const geometryFor = (from: number, to: number) => [
      { lat: from, lon: -120 },
      { lat: (from + to) / 2, lon: -120 },
      { lat: to, lon: -120 },
    ]
    return tripFixture({
      slug: 'mixed',
      name: 'Mixed',
      waypoints: [waypointFixture(47, -120), waypointFixture(48, -120), waypointFixture(49, -120)],
      legs: [
        tripLeg({
          intent: 'highway_connector',
          start_waypoint_index: 0,
          end_waypoint_index: 1,
          routed: routeLeg({ geometry: geometryFor(47, 48), intent: 'highway_connector' }),
        }),
        tripLeg({
          intent: 'unpaved',
          start_waypoint_index: 1,
          end_waypoint_index: 2,
          routed: routeLeg({ geometry: geometryFor(48, 49), intent: 'unpaved' }),
        }),
      ],
    })
  }

  /** Cheap engine live, metered engine preview-only — the real direction of the tradeoff. */
  const MIXED_CAPABILITIES: RoutingCapabilitiesResponse = {
    providers: [],
    intents: {
      highway_connector: intentRouting({ provider: 'google', live_update_interval_ms: 0 }),
      unpaved: intentRouting({ provider: 'ors', live_update_interval_ms: null }),
    },
  }

  async function loadedApp() {
    window.history.replaceState(null, '', '/?trip=mixed')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(mixedTrip())
    router.routingCapabilities.mockResolvedValue(MIXED_CAPABILITIES)
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    await waitFor(() => {
      expect(fake.polylines.filter((line) => line.map !== null).length).toBeGreaterThan(1)
    })
    return { fake, router }
  }

  /** The polyline drawn for a given leg, which is what the rider grabs. */
  function lineForLeg(fake: ReturnType<typeof createFakeMaps>, legIndex: number) {
    return fake.polylines.filter(
      (line) => line.map !== null && line.options['zIndex'] === 10,
    )[legIndex]
  }

  it('re-routes only the leg the rider grabbed', async () => {
    // What the whole multi-leg exercise was for. This used to re-request every waypoint of
    // the trip, which on Tim's 274 km route is what he felt as latency.
    const { fake, router } = await loadedApp()
    const map = fake.maps[0]
    router.routeLeg.mockClear()

    act(() => {
      lineForLeg(fake, 1)?.mouseDown({ lat: 48.5, lon: -120 })
    })
    act(() => {
      map?.mouseUp({ lat: 48.5, lon: -120.4 })
    })

    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(1))
    const request = router.routeLeg.mock.calls[0]?.[0]
    // The second leg's own two waypoints with the via between them. Not the trip's three.
    expect(request?.waypoints).toEqual([
      { lat: 48, lon: -120 },
      { lat: 48.5, lon: -120.4 },
      { lat: 49, lon: -120 },
    ])
    // And the leg's own mode, not a default: a drag must not retarmac a dirt section.
    expect(request?.intent).toBe('unpaved')
  })

  it('holds off during the gesture on a metered leg, and updates live on a cheap one', async () => {
    // Cadence follows the engine behind the leg under the cursor. One interval for the whole
    // trip means either a dirt leg burning a 2,000-a-day quota at highway speed, or a highway
    // leg feeling sluggish for a reason that does not apply to it.
    const { fake, router } = await loadedApp()
    const map = fake.maps[0]
    router.routeLeg.mockClear()

    // The dirt leg: preview-only, so moving spends nothing.
    act(() => {
      lineForLeg(fake, 1)?.mouseDown({ lat: 48.5, lon: -120 })
    })
    act(() => {
      map?.mouseMove({ lat: 48.5, lon: -120.2 })
    })
    expect(router.routeLeg).not.toHaveBeenCalled()
    act(() => {
      map?.mouseUp({ lat: 48.5, lon: -120.3 })
    })
    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(1))

    // The highway leg: live while the pointer moves.
    router.routeLeg.mockClear()
    act(() => {
      lineForLeg(fake, 0)?.mouseDown({ lat: 47.5, lon: -120 })
    })
    act(() => {
      map?.mouseMove({ lat: 47.5, lon: -120.2 })
    })

    await waitFor(() => expect(router.routeLeg).toHaveBeenCalledTimes(1))
    expect(router.routeLeg.mock.calls[0]?.[0].intent).toBe('highway_connector')
  })
})

describe('the assistant rail', () => {
  it('offers the assistant without a trip existing first', async () => {
    // The opening line invites describing a trip before placing anything, so the composer has
    // to be usable from a cold start or the app's own first sentence is decoration.
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    expect(screen.getByRole('textbox', { name: /ask the assistant/i })).not.toBeDisabled()
    expect(screen.getByText(/describe your trip/i)).toBeInTheDocument()
  })

  it('creates a trip for the first message and talks about that trip', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fireEvent.change(screen.getByRole('textbox', { name: /ask the assistant/i }), {
      target: { value: 'three days of dirt out of Leavenworth' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(router.createTrip).toHaveBeenCalledTimes(1))
    const created = router.createTrip.mock.calls[0]?.[0].slug
    await waitFor(() => expect(router.chat).toHaveBeenCalledTimes(1))
    // The same document the mouse would have created, not a second one.
    expect(router.chat.mock.calls[0]?.[0]).toBe(created)
  })

  it('re-reads the trip when the assistant edits it', async () => {
    // The rule the whole design turns on: one document, read back, never reconstructed from
    // the event stream. Two models of one trip diverge silently.
    window.history.replaceState(null, '', '/?trip=wabdr-north')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'WABDR North' }))
    router.chat.mockImplementation(
      // eslint-disable-next-line @typescript-eslint/require-await
      async function* () {
        yield { kind: 'done' as const, message: '', tool: null, trip_changed: true, truncated: false }
      },
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    await waitFor(() => expect(router.getTrip).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByRole('textbox', { name: /ask the assistant/i }), {
      target: { value: 'add a campground' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(router.getTrip).toHaveBeenCalledTimes(2))
  })

  it('says the assistant is not built yet rather than reporting a failure', async () => {
    // It 501s today. Presenting that as an error trains a rider to distrust the rail once it
    // does work.
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    fireEvent.change(screen.getByRole('textbox', { name: /ask the assistant/i }), {
      target: { value: 'hello' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(await screen.findByText(/not built yet/i)).toBeInTheDocument()
  })
})

describe('a trip made of legs', () => {
  it('asks only about the leg the rider just added', async () => {
    // A trip used to be one leg spanning every waypoint, so the fourth click re-routed the
    // whole thing: the wait and the bill both grew with the length of the trip, and "re-request
    // the affected leg only" was honest right up to the point where the affected leg was all
    // 274 km of it.
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fake.clickMap(47.0, -120.0)
    fake.clickMap(47.5, -120.5)
    fake.clickMap(48.0, -121.0)
    fake.clickMap(48.5, -121.5)

    await waitFor(() => {
      expect(router.routeLeg).toHaveBeenCalledTimes(3)
    })
    // Every request is a pair. Before this, they were 2, 3 and 4 waypoints long.
    for (const call of router.routeLeg.mock.calls) {
      expect(call[0].waypoints).toHaveLength(2)
    }
  })

  it('asks nothing again when the rider removes the point they just placed', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(47.5, -120.5)
    fake.clickMap(48.0, -121.0)
    await waitFor(() => {
      expect(router.routeLeg).toHaveBeenCalledTimes(2)
    })

    removeLastPoint()

    // The first leg is untouched, so there is nothing to ask. It used to cost a request for
    // the whole remaining route.
    await waitFor(() => {
      expect(screen.getByText(/2 points placed/)).toBeInTheDocument()
    })
    expect(router.routeLeg).toHaveBeenCalledTimes(2)
  })

  it('keeps the rest of the route when one segment cannot be routed', async () => {
    // Partial beats total. One dead segment is not a dead trip, and blanking the map throws
    // away work the rider can still use.
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.routeLeg.mockImplementation((request: RouteLegInput) =>
      request.waypoints[0]?.lat === 47.5
        ? Promise.reject(new ApiError({ status: 422, code: 'no_route_found', detail: 'no road' }))
        : Promise.resolve(ROUTE_RESPONSE),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fake.clickMap(47.0, -120.0)
    fake.clickMap(47.5, -120.5)
    fake.clickMap(48.0, -121.0)

    expect(await screen.findByText(/1 segment could not be routed/i)).toBeInTheDocument()
    // The first leg still has a line on the map.
    await waitFor(() => {
      expect(fake.polylines.filter((line) => line.map !== null).length).toBeGreaterThan(0)
    })
  })

  it('stops saying a segment failed once the rider removes it', async () => {
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.routeLeg.mockImplementation((request: RouteLegInput) =>
      request.waypoints[0]?.lat === 47.5
        ? Promise.reject(new ApiError({ status: 422, code: 'no_route_found', detail: 'no road' }))
        : Promise.resolve(ROUTE_RESPONSE),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.0, -120.0)
    fake.clickMap(47.5, -120.5)
    fake.clickMap(48.0, -121.0)
    await screen.findByText(/1 segment could not be routed/i)

    removeLastPoint()

    // A warning about a segment that no longer exists cannot be dismissed.
    await waitFor(() => {
      expect(screen.queryByText(/could not be routed/i)).not.toBeInTheDocument()
    })
  })
})

/**
 * Auto-select, and the door staying reachable.
 *
 * Tim asked for the only trip to open itself, and paid for the dead-end it creates: a
 * persistent New trip control, so "create is always reachable" even for the rider whose single
 * trip would otherwise swallow the entrance.
 */
describe('App with one trip already', () => {
  function withVisited(entries: readonly { slug: string; name: string }[]): void {
    localStorage.setItem('motorooter.visitedTrips', JSON.stringify(entries))
  }

  it('opens the only trip without asking', async () => {
    withVisited([{ slug: 'wabdr-north', name: 'WABDR North' }])
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'WABDR North' }))

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)

    await waitFor(() => expect(fake.maps).toHaveLength(1))
    expect(router.getTrip).toHaveBeenCalledWith('wabdr-north', expect.anything())
  })

  it('asks when there is more than one, because there is a choice to make', async () => {
    withVisited([
      { slug: 'a', name: 'Cascades loop' },
      { slug: 'b', name: 'WABDR North' },
    ])
    const fake = createFakeMaps()

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)

    expect(await screen.findByRole('button', { name: 'Cascades loop' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start a new trip/i })).toBeInTheDocument()
  })

  it('still honours a link, whatever is in the list', async () => {
    withVisited([{ slug: 'mine', name: 'Mine' }])
    window.history.replaceState(null, '', '/?trip=someone-elses')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'someone-elses', name: "Someone else's" }))

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)

    await waitFor(() => expect(fake.maps).toHaveLength(1))
    // The link wins, and nothing else is opened. Asserting only that the link was fetched
    // passed even with the URL guard removed: auto-select fetched 'mine' as well, and the two
    // races were invisible to an assertion that just asked whether the link was among them.
    await waitFor(() => {
      expect(router.getTrip).toHaveBeenCalledWith('someone-elses', expect.anything())
    })
    expect(router.getTrip.mock.calls.map((call) => call[0])).toEqual(['someone-elses'])
    expect(new URL(window.location.href).searchParams.get('trip')).toBe('someone-elses')
  })

  it('keeps creating a trip one click away from the map', async () => {
    // The dead end auto-select would otherwise create: one trip, and no way back to the door.
    withVisited([{ slug: 'wabdr-north', name: 'WABDR North' }])
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'WABDR North' }))
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))

    fireEvent.click(screen.getByRole('button', { name: 'New trip' }))

    expect(await screen.findByRole('button', { name: /start a new trip/i })).toBeInTheDocument()
  })

  it('does not write the new trip over the one it left', async () => {
    // The bug this exists to catch: New trip left the previous document loaded, so the first
    // waypoint of the *next* trip was PUT to the previous trip's slug — silently replacing a
    // trip the rider had just been looking at.
    window.history.replaceState(null, '', '/?trip=old-one')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'old-one',
        name: 'Old one',
        waypoints: [waypointFixture(47.5, -120.5)],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    await screen.findByRole('heading', { name: 'Old one' })

    fireEvent.click(screen.getByRole('button', { name: 'New trip' }))
    fireEvent.click(await screen.findByRole('button', { name: /start a new trip/i }))
    // The *second* map, not just any map: the remount builds a new one, and the fake's click
    // handler belongs to whichever was built last. Waiting on `length >= 1` was already true
    // from the first mount, so the click went to the tree that had just been thrown away.
    await waitFor(() => {
      expect(fake.maps).toHaveLength(2)
    })
    fake.clickMap(46.1, -121.1)

    await waitFor(() => expect(router.createTrip).toHaveBeenCalled(), { timeout: 3000 })
    // Written to the trip it just created, and to nothing else. Asserted positively as well:
    // "never old-one" alone would hold just as well if no write happened at all.
    const created = router.createTrip.mock.calls[0]?.[0].slug
    await waitFor(() => expect(router.updateTrip).toHaveBeenCalled(), { timeout: 3000 })
    for (const call of router.updateTrip.mock.calls) expect(call[0]).toBe(created)
  })

  it('starts the new trip empty rather than carrying the last one into it', async () => {
    window.history.replaceState(null, '', '/?trip=old-one')
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(
      tripFixture({
        slug: 'old-one',
        name: 'Old one',
        waypoints: [
          waypointFixture(47.5, -120.5),
          waypointFixture(47.9, -120.1),
        ],
      }),
    )
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    await waitFor(() => expect(attachedPins(fake).length).toBeGreaterThanOrEqual(2))

    fireEvent.click(screen.getByRole('button', { name: 'New trip' }))
    fireEvent.click(await screen.findByRole('button', { name: /start a new trip/i }))

    // A blank map: the previous trip's waypoints are not the new trip's waypoints.
    await waitFor(() => {
      expect(attachedPins(fake)).toHaveLength(0)
    })
    expect(screen.queryByRole('heading', { name: 'Old one' })).not.toBeInTheDocument()
  })

  it('does not auto-open again after the rider asked for a new one', async () => {
    // Otherwise New trip bounces straight back into the trip it just left, which is the same
    // dead end wearing a button.
    withVisited([{ slug: 'wabdr-north', name: 'WABDR North' }])
    const fake = createFakeMaps()
    const router = fakeRouter()
    router.getTrip.mockResolvedValue(tripFixture({ slug: 'wabdr-north', name: 'WABDR North' }))
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))

    fireEvent.click(screen.getByRole('button', { name: 'New trip' }))
    await screen.findByRole('button', { name: /start a new trip/i })

    // Still at the door a tick later, rather than having been pulled back.
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.getByRole('button', { name: /start a new trip/i })).toBeInTheDocument()
    expect(new URL(window.location.href).searchParams.get('trip')).toBeNull()
  })
})
