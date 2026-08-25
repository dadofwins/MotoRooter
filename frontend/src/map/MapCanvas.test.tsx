import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MapCanvas } from './MapCanvas'
import type { GoogleMaps } from './loadGoogleMaps'
import type { Coordinate, TripLeg, Waypoint } from '../api/types'

/**
 * The canvas is where a Maps API the tests cannot load meets React's lifecycle. Two classes
 * of bug live here and both are invisible until they are bad: overlays that are recreated
 * without the old ones being detached (the map slows to a crawl over a long editing
 * session), and a map that gets rebuilt on a prop change (the viewport jumps out from under
 * a user mid-pan).
 *
 * The Maps API is faked at the namespace boundary — the same seam the loader resolves.
 */

interface FakeListener {
  readonly event: string
  readonly handler: (event: unknown) => void
  removed: boolean
}

function createFakeMaps() {
  const maps: FakeMap[] = []
  const polylines: FakePolyline[] = []
  const markers: FakeMarker[] = []

  class FakeLatLngBounds {
    readonly extended: google.maps.LatLngLiteral[] = []
    extend(point: google.maps.LatLngLiteral): this {
      this.extended.push(point)
      return this
    }
  }

  class FakeMap {
    readonly listeners: FakeListener[] = []
    readonly fitted: FakeLatLngBounds[] = []
    constructor(
      readonly element: HTMLElement,
      readonly options: google.maps.MapOptions,
    ) {
      maps.push(this)
    }
    addListener(event: string, handler: (event: unknown) => void): { remove: () => void } {
      const listener: FakeListener = { event, handler, removed: false }
      this.listeners.push(listener)
      return {
        remove: () => {
          listener.removed = true
        },
      }
    }
    fitBounds(bounds: FakeLatLngBounds): void {
      this.fitted.push(bounds)
    }
    /** Drives a click the way the API would. */
    click(coordinate: Coordinate): void {
      for (const listener of this.listeners.filter((l) => l.event === 'click' && !l.removed)) {
        listener.handler({ latLng: { lat: () => coordinate.lat, lng: () => coordinate.lon } })
      }
    }
  }

  class FakePolyline {
    map: unknown = null
    constructor(readonly options: google.maps.PolylineOptions) {
      this.map = options.map ?? null
      polylines.push(this)
    }
    setMap(map: unknown): void {
      this.map = map
    }
  }

  class FakeMarker {
    map: unknown
    constructor(readonly options: Record<string, unknown>) {
      this.map = options['map'] ?? null
      markers.push(this)
    }
  }

  const namespace = {
    Map: FakeMap,
    Polyline: FakePolyline,
    LatLngBounds: FakeLatLngBounds,
    marker: { AdvancedMarkerElement: FakeMarker },
  }

  return {
    loader: () => Promise.resolve(namespace as unknown as GoogleMaps),
    maps,
    polylines,
    markers,
    /** Overlays still attached to a map — anything left here after unmount is a leak. */
    attached: () => [...polylines, ...markers].filter((overlay) => overlay.map !== null),
  }
}

function coords(count: number, lat = 47): Coordinate[] {
  return Array.from({ length: count }, (_, index) => ({ lat: lat + index / 100, lon: -120 }))
}

function leg(geometry: Coordinate[]): TripLeg {
  return {
    intent: 'unpaved',
    start_waypoint_index: 0,
    end_waypoint_index: 1,
    provider_override: null,
    routed: {
      geometry,
      distance_m: 1,
      duration_s: 1,
      provider: 'fake',
      intent: 'unpaved',
      surface_spans: [],
      ascent_m: null,
    },
  }
}

function waypoint(lat: number, name: string | null = null): Waypoint {
  return { coordinate: { lat, lon: -120 }, name, pinned: true }
}

describe('MapCanvas', () => {
  it('says it is loading rather than showing an unexplained empty pane', () => {
    // A never-resolving loader stands in for a slow connection.
    render(<MapCanvas loader={() => new Promise(() => undefined)} />)

    expect(screen.getByRole('status')).toHaveTextContent(/loading/i)
  })

  it('creates exactly one map, with our options', async () => {
    const fake = createFakeMaps()

    render(<MapCanvas loader={fake.loader} mapId="vector-id" zoom={9} center={{ lat: 47, lon: -120 }} />)

    await waitFor(() => expect(fake.maps).toHaveLength(1))
    const options = fake.maps[0]?.options
    expect(options?.zoom).toBe(9)
    expect(options?.mapId).toBe('vector-id')
    expect(options?.renderingType).toBe('VECTOR')
    expect(options?.clickableIcons).toBe(false)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows why the map failed instead of a blank grey rectangle', async () => {
    const load = () => Promise.reject(new Error('No Google Maps browser key. Set VITE_...'))

    render(<MapCanvas loader={load} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/No Google Maps browser key/)
  })

  it('draws one polyline per surface segment, in Google’s coordinate naming', async () => {
    const fake = createFakeMaps()

    render(<MapCanvas loader={fake.loader} legs={[leg(coords(3))]} />)

    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    expect(fake.polylines[0]?.options.path).toEqual([
      { lat: 47, lng: -120 },
      { lat: 47.01, lng: -120 },
      { lat: 47.02, lng: -120 },
    ])
  })

  it('detaches the previous polylines when the route changes', async () => {
    // Without this the map accumulates every version of every leg the user has dragged
    // through, and an hour of editing turns it to treacle.
    const fake = createFakeMaps()
    const { rerender } = render(<MapCanvas loader={fake.loader} legs={[leg(coords(3))]} />)
    await waitFor(() => expect(fake.polylines).toHaveLength(1))

    rerender(<MapCanvas loader={fake.loader} legs={[leg(coords(4))]} />)

    await waitFor(() => expect(fake.polylines).toHaveLength(2))
    expect(fake.polylines[0]?.map).toBeNull()
    expect(fake.polylines[1]?.map).not.toBeNull()
  })

  it('never rebuilds the map when props change', async () => {
    // Recreating it would reset zoom and centre while the user is looking at them.
    const fake = createFakeMaps()
    const { rerender } = render(<MapCanvas loader={fake.loader} legs={[]} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))

    rerender(<MapCanvas loader={fake.loader} legs={[leg(coords(3))]} />)
    rerender(<MapCanvas loader={fake.loader} legs={[leg(coords(5))]} zoom={12} />)

    await waitFor(() => expect(fake.polylines.length).toBeGreaterThan(1))
    expect(fake.maps).toHaveLength(1)
  })

  it('marks every waypoint, labelling the ends differently from the middle', async () => {
    const fake = createFakeMaps()

    render(
      <MapCanvas
        loader={fake.loader}
        waypoints={[waypoint(47), waypoint(48), waypoint(49, 'Sun Mountain Lodge')]}
      />,
    )

    await waitFor(() => expect(fake.markers).toHaveLength(3))
    const labels = fake.markers.map((marker) =>
      (marker.options['content'] as HTMLElement).getAttribute('aria-label'),
    )
    expect(labels).toEqual(['Start', 'Via point', 'End: Sun Mountain Lodge'])
  })

  it('reports a map click as a domain coordinate — the mouse path for setting points', async () => {
    // Chat is an accelerator, never a requirement: setting start and end with the mouse
    // has to work on its own.
    const fake = createFakeMaps()
    const onMapClick = vi.fn()

    render(<MapCanvas loader={fake.loader} onMapClick={onMapClick} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))
    fake.maps[0]?.click({ lat: 47.5, lon: -120.25 })

    expect(onMapClick).toHaveBeenCalledWith({ lat: 47.5, lon: -120.25 })
  })

  it('keeps calling the latest click handler without rebinding the map listener', async () => {
    const fake = createFakeMaps()
    const first = vi.fn()
    const second = vi.fn()
    const { rerender } = render(<MapCanvas loader={fake.loader} onMapClick={first} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))

    rerender(<MapCanvas loader={fake.loader} onMapClick={second} />)
    fake.maps[0]?.click({ lat: 1, lon: 2 })

    expect(second).toHaveBeenCalledWith({ lat: 1, lon: 2 })
    expect(first).not.toHaveBeenCalled()
    expect(fake.maps[0]?.listeners.filter((l) => l.event === 'click')).toHaveLength(1)
  })

  it('leaves nothing attached to the map after unmount', async () => {
    const fake = createFakeMaps()
    const { unmount } = render(
      <MapCanvas loader={fake.loader} legs={[leg(coords(3))]} waypoints={[waypoint(47)]} />,
    )
    await waitFor(() => expect(fake.attached()).not.toHaveLength(0))

    unmount()

    expect(fake.attached()).toEqual([])
    expect(fake.maps[0]?.listeners.every((listener) => listener.removed)).toBe(true)
  })

  it('builds no map at all when unmounted before the API arrives', async () => {
    // Navigating away during a slow load must not leave a map bound to a detached node.
    const fake = createFakeMaps()
    let release = (): void => undefined
    const load = async (): Promise<GoogleMaps> => {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return fake.loader()
    }

    const { unmount } = render(<MapCanvas loader={load} />)
    unmount()
    release()

    await waitFor(() => expect(fake.maps).toHaveLength(0))
  })

  it('ignores a superseded load, so a stale failure cannot cover a working map', async () => {
    // The stale-response problem again. A parent that re-renders with a different loader
    // leaves the first one in flight; if its rejection is allowed to land afterwards, an
    // error is shown over a map that loaded perfectly well.
    const fake = createFakeMaps()
    let failFirstLoad = (): void => undefined
    const stalled = (): Promise<GoogleMaps> =>
      new Promise<GoogleMaps>((_, reject) => {
        failFirstLoad = () => {
          reject(new Error('offline'))
        }
      })

    const { rerender } = render(<MapCanvas loader={stalled} />)
    rerender(<MapCanvas loader={fake.loader} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))

    failFirstLoad()
    // Let the rejection actually settle. Waiting for an *absence* would otherwise pass
    // instantly, before the state update it is meant to rule out could even happen.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('frames the route the first time geometry arrives, and not again afterwards', async () => {
    // Fitting on every update would fight a user who has panned somewhere deliberately.
    const fake = createFakeMaps()
    const { rerender } = render(<MapCanvas loader={fake.loader} legs={[]} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))
    expect(fake.maps[0]?.fitted).toHaveLength(0)

    rerender(<MapCanvas loader={fake.loader} legs={[leg(coords(3))]} />)
    await waitFor(() => expect(fake.maps[0]?.fitted).toHaveLength(1))
    expect(fake.maps[0]?.fitted[0]?.extended).toHaveLength(3)

    rerender(<MapCanvas loader={fake.loader} legs={[leg(coords(6))]} />)
    await waitFor(() => expect(fake.polylines.length).toBeGreaterThan(1))
    expect(fake.maps[0]?.fitted).toHaveLength(1)
  })

  it('offers a retry when loading failed, since a dropped connection is not permanent', async () => {
    const fake = createFakeMaps()
    let attempt = 0
    const load = (): Promise<GoogleMaps> => {
      attempt += 1
      return attempt === 1 ? Promise.reject(new Error('offline')) : fake.loader()
    }

    render(<MapCanvas loader={load} />)
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: /try again/i }))

    await waitFor(() => expect(fake.maps).toHaveLength(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
