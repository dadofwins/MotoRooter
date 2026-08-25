import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { ApiError } from './api/errors'
import type { RequestOptions } from './api/client'
import type { GoogleMaps } from './map/loadGoogleMaps'
import type { Coordinate, RouteLegInput, RouteLegResponse, RoutingCapabilitiesResponse } from './api/types'

/**
 * The shell, and the one rule it has to prove: **chat is an accelerator, never a
 * requirement.** Placing the start and end of a trip has to work with nothing but the
 * mouse, so these tests drive the map rather than the chat rail.
 */

const PIN_PROBE_ID = 'pin-probe'

/** The shape Maps delivers position in. */
function latLngEvent(coordinate: Coordinate): unknown {
  return { latLng: { lat: () => coordinate.lat, lng: () => coordinate.lon } }
}

const CAPABILITIES: RoutingCapabilitiesResponse = {
  providers: [],
  intents: { unpaved: { provider: 'ors', live_update_interval_ms: 0 } },
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
        constructor(readonly options: Record<string, unknown>) {
          this.map = options['map'] ?? null
          markers.push(this)
        }
      },
    },
  }

  return {
    loader: () => Promise.resolve(namespace as unknown as GoogleMaps),
    maps,
    markers,
    polylines,
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
 * Waits until the map object exists, not merely until the loading text has gone.
 *
 * They are not the same moment. The text disappears when React commits the state change,
 * while the map is constructed in a passive effect that can flush a tick later — so a test
 * that waits for the text and then clicks occasionally finds no click listener yet. Rare
 * enough to look like a heisenbug and frequent enough to fail CI.
 */
async function mapReady(fake: ReturnType<typeof createFakeMaps>): Promise<void> {
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

const ROUTE_RESPONSE: RouteLegResponse = {
  leg: {
    geometry: [
      { lat: 47.6, lon: -120.7 },
      { lat: 47.9, lon: -120.4 },
      { lat: 48.1, lon: -120.2 },
    ],
    distance_m: 42_000,
    duration_s: 3600,
    provider: 'fake',
    intent: 'twisty_paved',
    surface_spans: [],
    ascent_m: null,
  },
  live_update_interval_ms: 0,
}

function fakeRouter(response: RouteLegResponse = ROUTE_RESPONSE) {
  return {
    routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) => Promise.resolve(response)),
    routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(CAPABILITIES)),
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

    expect(await screen.findByText(/42 km/i)).toBeInTheDocument()
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
    expect(await screen.findByText(/42 km/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /remove last point/i }))

    await waitFor(() => expect(fake.polylines.every((line) => line.map === null)).toBe(true))
    expect(screen.queryByText(/42 km/i)).not.toBeInTheDocument()
  })

  it('drops a routing error once the points that caused it are gone', async () => {
    const fake = createFakeMaps()
    const router = {
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(new ApiError({ status: 422, code: 'no_route_found', detail: 'nope' })),
      ),
      routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(CAPABILITIES)),
    }
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await screen.findByRole('alert')

    fireEvent.click(screen.getByRole('button', { name: /remove last point/i }))

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('never shows an internal error string to the rider', async () => {
    const fake = createFakeMaps()
    const router = {
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
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(new ApiError({ status: 422, code: 'no_route_found', detail: 'no route found' })),
      ),
      routingCapabilities: vi.fn((_options?: RequestOptions) => Promise.resolve(CAPABILITIES)),
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
      routeLeg: vi.fn((request: RouteLegInput, _options?: RequestOptions) =>
        Promise.resolve({
          leg: {
            ...ROUTE_RESPONSE.leg,
            // Echo the request back, so a leg is recognisably the route that was asked for.
            geometry: [...request.waypoints],
            routed_from: { intent: request.intent, waypoints: [...request.waypoints] },
          },
          live_update_interval_ms: 0,
        }),
      ),
      routingCapabilities: vi.fn((_options?: RequestOptions) =>
        Promise.resolve(CAPABILITIES),
      ),
    }

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await mapReady(fake)
    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    return { fake, router }
  }

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

    expect(screen.getByText(/describe your trip/i)).toBeInTheDocument()
    expect(screen.getByText(/set a start and end point on the map/i)).toBeInTheDocument()
    // Let the map finish loading before the test ends, or its state update lands on an
    // unmounted tree and React warns about it.
    await mapReady(fake)
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

  it('offers an undo for a misplaced point, and only when there is one to undo', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await mapReady(fake)

    expect(screen.queryByRole('button', { name: /remove last point/i })).not.toBeInTheDocument()

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    expect(await pinLabels(fake, 2)).toHaveLength(2)

    fireEvent.click(screen.getByRole('button', { name: /remove last point/i }))

    expect(await pinLabels(fake, 1)).toEqual(['Start'])
  })
})
