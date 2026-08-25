import { render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { ApiError } from './api/errors'
import type { RequestOptions } from './api/client'
import type { GoogleMaps } from './map/loadGoogleMaps'
import type { RouteLegInput, RouteLegResponse } from './api/types'

/**
 * The shell, and the one rule it has to prove: **chat is an accelerator, never a
 * requirement.** Placing the start and end of a trip has to work with nothing but the
 * mouse, so these tests drive the map rather than the chat rail.
 */

const PIN_PROBE_ID = 'pin-probe'

interface FakeMarker {
  readonly options: Record<string, unknown>
  /** Set to null when the marker is detached, which is how a removed pin disappears. */
  map: unknown
}

function createFakeMaps() {
  const markers: FakeMarker[] = []
  const polylines: { options: Record<string, unknown> }[] = []
  let clickHandler: ((event: unknown) => void) | null = null

  class FakeMap {
    addListener(event: string, handler: (event: unknown) => void): { remove: () => void } {
      if (event === 'click') clickHandler = handler
      return { remove: () => undefined }
    }
    fitBounds(): void {
      // Nothing to assert here; framing is covered in MapCanvas's own tests.
    }
  }

  const namespace = {
    Map: FakeMap,
    Polyline: class {
      constructor(readonly options: Record<string, unknown>) {
        polylines.push(this)
      }
      setMap(): void {}
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
    markers,
    polylines,
    clickMap(lat: number, lon: number): void {
      if (clickHandler === null) throw new Error('the map has no click listener')
      clickHandler({ latLng: { lat: () => lat, lng: () => lon } })
    },
  }
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
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

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
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    fake.clickMap(47.6, -120.7)
    await waitFor(() => expect(attachedPins(fake)).toHaveLength(1))

    expect(router.routeLeg).not.toHaveBeenCalled()
  })

  it('reports the routed distance, so the number comes from the server not the screen', async () => {
    const fake = createFakeMaps()
    render(
      <App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />,
    )
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)

    expect(await screen.findByText(/42 km/i)).toBeInTheDocument()
  })

  it('says when a route cannot be found instead of leaving the map silently empty', async () => {
    const fake = createFakeMaps()
    const router = {
      routeLeg: vi.fn((_request: RouteLegInput, _options?: RequestOptions) =>
        Promise.reject(new ApiError({ status: 422, code: 'no_route_found', detail: 'no route found' })),
      ),
    }
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={router} />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)

    expect(await screen.findByRole('alert')).toHaveTextContent(/no route/i)
  })
})

describe('App', () => {
  it('opens by telling the user both ways of starting', () => {
    const fake = createFakeMaps()

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)

    expect(screen.getByText(/describe your trip/i)).toBeInTheDocument()
    expect(screen.getByText(/set a start and end point on the map/i)).toBeInTheDocument()
  })

  it('places the start and the end from map clicks alone', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

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
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    fake.clickMap(47.6, -120.7)

    expect(await screen.findByText(/1 point/i)).toBeInTheDocument()
  })

  it('offers an undo for a misplaced point, and only when there is one to undo', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" client={fakeRouter()} />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    expect(screen.queryByRole('button', { name: /remove last point/i })).not.toBeInTheDocument()

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    expect(await pinLabels(fake, 2)).toHaveLength(2)

    screen.getByRole('button', { name: /remove last point/i }).click()

    expect(await pinLabels(fake, 1)).toEqual(['Start'])
  })
})
