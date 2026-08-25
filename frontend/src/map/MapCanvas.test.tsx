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

/** Delivers a Maps event to every live listener registered for it. */
function emit(listeners: readonly FakeListener[], event: string, coordinate: Coordinate): void {
  for (const listener of listeners.filter((l) => l.event === event && !l.removed)) {
    listener.handler({ latLng: { lat: () => coordinate.lat, lng: () => coordinate.lon } })
  }
}

function createFakeMaps({ withMarkerLibrary = true }: { withMarkerLibrary?: boolean } = {}) {
  const maps: FakeMap[] = []
  const polylines: FakePolyline[] = []
  const markers: FakeMarker[] = []
  const legacyMarkers: FakeLegacyMarker[] = []

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
    readonly applied: google.maps.MapOptions[] = []
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
    #zoom = 12
    getZoom(): number {
      return this.#zoom
    }
    setZoom(zoom: number): void {
      this.#zoom = zoom
    }
    setOptions(options: google.maps.MapOptions): void {
      this.applied.push(options)
    }
    /** Whether the user could pan the map right now. */
    get draggable(): boolean {
      return this.applied.reduce<boolean>(
        (value, options) => options.draggable ?? value,
        this.options.draggable ?? true,
      )
    }
    /** Drives a click the way the API would. */
    click(coordinate: Coordinate): void {
      emit(this.listeners, 'click', coordinate)
    }
    mouseMove(coordinate: Coordinate): void {
      emit(this.listeners, 'mousemove', coordinate)
    }
    mouseUp(coordinate: Coordinate): void {
      emit(this.listeners, 'mouseup', coordinate)
    }
  }

  class FakePolyline {
    map: unknown = null
    path: google.maps.LatLngLiteral[]
    readonly listeners: FakeListener[] = []
    constructor(readonly options: google.maps.PolylineOptions) {
      this.map = options.map ?? null
      this.path = (options.path ?? []) as google.maps.LatLngLiteral[]
      polylines.push(this)
    }
    setMap(map: unknown): void {
      this.map = map
    }
    setPath(path: google.maps.LatLngLiteral[]): void {
      this.path = path
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
    /** Drives a press on the line the way the API would. */
    mouseDown(coordinate: Coordinate): void {
      emit(this.listeners, 'mousedown', coordinate)
    }
  }

  class FakeMarker {
    map: unknown
    position: unknown
    constructor(readonly options: Record<string, unknown>) {
      this.map = options['map'] ?? null
      this.position = options['position'] ?? null
      markers.push(this)
    }
  }

  /** The deprecated `google.maps.Marker`, which unlike the advanced one needs no Map ID. */
  class FakeLegacyMarker {
    map: unknown
    position: unknown
    constructor(readonly options: Record<string, unknown>) {
      this.map = options['map'] ?? null
      this.position = options['position'] ?? null
      legacyMarkers.push(this)
    }
    setMap(map: unknown): void {
      this.map = map
    }
    setPosition(position: unknown): void {
      this.position = position
    }
  }

  const namespace = {
    Map: FakeMap,
    Polyline: FakePolyline,
    LatLngBounds: FakeLatLngBounds,
    Marker: FakeLegacyMarker,
    ...(withMarkerLibrary ? { marker: { AdvancedMarkerElement: FakeMarker } } : {}),
  }

  return {
    loader: () => Promise.resolve(namespace as unknown as GoogleMaps),
    maps,
    polylines,
    markers,
    legacyMarkers,
    /** Overlays still attached to a map — anything left here after unmount is a leak. */
    attached: () =>
      [...polylines, ...markers, ...legacyMarkers].filter((overlay) => overlay.map !== null),
    /** The live drag handle, if the gesture is showing one. */
    handles: () =>
      [...markers, ...legacyMarkers].filter(
        (marker) =>
          marker.map !== null &&
          (marker.options['content'] as HTMLElement | undefined)?.className?.includes(
            'drag-handle',
          ) === true,
      ),
  }
}

/** Any non-empty vector Map ID; the canvas only checks that one is configured. */
const MAP_ID = 'motorooter-test-vector'

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

/**
 * Dragging the route line.
 *
 * Google's DirectionsRenderer only makes routes *it* computed draggable, and ours come from
 * ORS, so the gesture is hand-built: press the line, move, release. What this layer owes the
 * rest of the app is only the three events with real coordinates — the throttling, the
 * stale-response rejection and the trip arithmetic all live elsewhere and are tested there.
 *
 * The failure that matters most here is a gesture that never ends: map panning stays off and
 * the map is dead to the touch, with a page reload as the only way out.
 */
describe('MapCanvas dragging the route', () => {
  async function dragging(
    handlers: {
      onLegGrab?: (legIndex: number, at: Coordinate) => boolean
      onLegDrag?: (at: Coordinate) => void
      onLegDrop?: (at: Coordinate) => void
    } = {},
  ) {
    const fake = createFakeMaps()
    const onLegGrab = vi.fn(handlers.onLegGrab ?? (() => true))
    const onLegDrag = vi.fn(handlers.onLegDrag)
    const onLegDrop = vi.fn(handlers.onLegDrop)

    const view = render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        // Waypoints at the ends of leg 0's geometry, so a via dropped on it has real
        // neighbours to sit between.
        waypoints={[waypoint(47), waypoint(47.02)]}
        legs={[leg(coords(3, 47)), leg(coords(3, 48))]}
        onLegGrab={onLegGrab}
        onLegDrag={onLegDrag}
        onLegDrop={onLegDrop}
      />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(2))
    return { fake, onLegGrab, onLegDrag, onLegDrop, view }
  }

  /**
   * The frame-rate half of the gesture.
   *
   * A 1 Hz routed update is a fine cadence, but nothing moving between updates reads as a
   * broken app rather than a thrifty one. So a handle follows the cursor locally, at pointer
   * speed, with no request and — critically — no trip through the state that feeds routing.
   * Routing that through React is what made the drag session rebuild itself last round.
   *
   * The handle alone, deliberately. Straight tangents from the route to the cursor were
   * tried and removed: they fan off a curved line and read as a second, competing route
   * rather than as feedback about the one being dragged.
   */
  describe('the drag handle', () => {
    it('puts a handle on the map when the line is grabbed, and nothing else', async () => {
      const { fake } = await dragging()
      expect(fake.handles()).toHaveLength(0)
      const drawnBefore = fake.polylines.length

      act(() => {
        fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      })

      expect(fake.handles()).toHaveLength(1)
      // No extra line: the dot carries "you are holding this point" on its own.
      expect(fake.polylines).toHaveLength(drawnBefore)
    })

    it('moves with the cursor without building anything new', async () => {
      // Rebuilding overlays per pointer event is the difference between a gesture that
      // tracks the hand and one that stutters.
      const { fake } = await dragging()
      const map = fake.maps[0]
      act(() => {
        fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      })
      const overlaysAfterGrab = fake.polylines.length

      act(() => {
        map?.mouseMove({ lat: 47.01, lon: -120.1 })
        map?.mouseMove({ lat: 47.01, lon: -120.2 })
        map?.mouseMove({ lat: 47.01, lon: -120.3 })
      })

      expect(fake.polylines).toHaveLength(overlaysAfterGrab)
      expect(fake.handles()[0]?.position).toEqual({ lat: 47.01, lng: -120.3 })
    })

    it('clears it on release, on cancel, and on unmount', async () => {
      const { fake, view } = await dragging()
      const map = fake.maps[0]

      act(() => {
        fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
        map?.mouseMove({ lat: 47.01, lon: -120.3 })
        map?.mouseUp({ lat: 47.01, lon: -120.3 })
      })
      expect(fake.handles()).toHaveLength(0)

      act(() => {
        fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
        map?.mouseUp({ lat: 47.01, lon: -120 }) // below threshold: cancelled
      })
      expect(fake.handles()).toHaveLength(0)

      act(() => {
        fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      })
      view.unmount()
      expect(fake.handles()).toHaveLength(0)
    })
  })

  it('reports which leg was grabbed, and where', async () => {
    const { fake, onLegGrab } = await dragging()

    act(() => {
      fake.polylines[1]?.mouseDown({ lat: 48.01, lon: -120 })
    })

    // The second polyline belongs to the second leg: dragging must re-request that leg
    // alone, so the index has to survive the trip through the map layer.
    expect(onLegGrab).toHaveBeenCalledWith(1, { lat: 48.01, lon: -120 })
  })

  it('holds the map still while the line is being dragged, and lets go afterwards', async () => {
    // Without this the basemap pans under the cursor and the route runs away from it.
    const { fake } = await dragging()
    const map = fake.maps[0]
    expect(map?.draggable).toBe(true)

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
    })
    expect(map?.draggable).toBe(false)

    act(() => {
      map?.mouseUp({ lat: 47.01, lon: -120.2 })
    })
    expect(map?.draggable).toBe(true)
  })

  it('reports movement only while a drag is in progress', async () => {
    const { fake, onLegDrag } = await dragging()
    const map = fake.maps[0]

    act(() => {
      map?.mouseMove({ lat: 47.5, lon: -120 })
    })
    expect(onLegDrag).not.toHaveBeenCalled() // just a cursor crossing the map

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      map?.mouseMove({ lat: 47.01, lon: -120.1 })
    })
    expect(onLegDrag).toHaveBeenCalledWith({ lat: 47.01, lon: -120.1 })
  })

  it('ends the gesture on release and ignores what happens after', async () => {
    const { fake, onLegDrag, onLegDrop } = await dragging()
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      map?.mouseUp({ lat: 47.01, lon: -120.3 })
    })
    expect(onLegDrop).toHaveBeenCalledWith({ lat: 47.01, lon: -120.3 })

    act(() => {
      map?.mouseMove({ lat: 47.01, lon: -120.9 })
    })
    expect(onLegDrag).not.toHaveBeenCalled()
  })

  it('starts nothing when the grab is refused', async () => {
    // DragSession refuses a leg it cannot place a via-point on — an unrouted one, with no
    // line drawn and no way to tell where along it the press landed.
    const { fake, onLegDrag } = await dragging({ onLegGrab: () => false })
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
    })

    expect(map?.draggable).toBe(true)
    act(() => {
      map?.mouseMove({ lat: 47.01, lon: -120.1 })
    })
    expect(onLegDrag).not.toHaveBeenCalled()
  })

  it('ends the gesture even when the button is released off the map', async () => {
    // Release outside the map and Google's mouseup never fires. Without a backstop the
    // gesture never ends: panning stays disabled and the map is dead until a reload.
    const { fake, onLegDrop } = await dragging()
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      map?.mouseMove({ lat: 47.01, lon: -120.2 }) // last position the map saw
    })
    act(() => {
      window.dispatchEvent(new MouseEvent('mouseup'))
    })

    // Committed at the last known position rather than abandoned: the rider let go meaning
    // to place the point there.
    expect(onLegDrop).toHaveBeenCalledWith({ lat: 47.01, lon: -120.2 })
    expect(map?.draggable).toBe(true)
  })

  it('swallows the click Google emits after a drag, but only that one', async () => {
    // Releasing the line produces a click as well as a mouseup. Left alone it drops a new
    // waypoint wherever the drag finished. Swallowing every click afterwards would be the
    // worse bug: the map would stop accepting points entirely, with no sign why.
    const fake = createFakeMaps()
    const onMapClick = vi.fn()
    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        legs={[leg(coords(3))]}
        onMapClick={onMapClick}
        onLegGrab={() => true}
      />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      map?.mouseMove({ lat: 47.01, lon: -120.2 })
      map?.mouseUp({ lat: 47.01, lon: -120.2 })
      map?.click({ lat: 47.01, lon: -120.2 }) // Google's post-drag click
    })
    expect(onMapClick).not.toHaveBeenCalled()

    act(() => {
      map?.click({ lat: 49, lon: -121 }) // a real click, later
    })
    expect(onMapClick).toHaveBeenCalledWith({ lat: 49, lon: -121 })
  })

  it('treats a press-and-release on the line as a click, not a drag', async () => {
    // Without a movement threshold, touching the line spends a routing request and pins a
    // waypoint the rider never asked for — invisible, because the via lands on the line
    // that is already there.
    const fake = createFakeMaps()
    const onLegDrop = vi.fn()
    const onLegCancel = vi.fn()
    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        legs={[leg(coords(3))]}
        onLegGrab={() => true}
        onLegDrop={onLegDrop}
        onLegCancel={onLegCancel}
      />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      map?.mouseUp({ lat: 47.01, lon: -120 }) // same place
    })

    expect(onLegDrop).not.toHaveBeenCalled()
    expect(onLegCancel).toHaveBeenCalledTimes(1)
    expect(map?.draggable).toBe(true) // and the map is usable again
  })

  it('still treats a small but deliberate drag as a drag', async () => {
    // The threshold is in screen pixels, so it must not swallow a real drag just because
    // the map is zoomed in and a few pixels is only a few metres.
    const fake = createFakeMaps()
    const onLegDrop = vi.fn()
    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        legs={[leg(coords(3))]}
        onLegGrab={() => true}
        onLegDrop={onLegDrop}
      />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    const map = fake.maps[0]
    map?.setZoom(18) // ~0.6 m per pixel

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      map?.mouseUp({ lat: 47.01, lon: -120.0002 }) // ~15 m: tens of pixels at this zoom
    })

    expect(onLegDrop).toHaveBeenCalledTimes(1)
  })

  it('does not eat the next click after a release outside the map', async () => {
    // Releasing over the chat rail ends the gesture through the window backstop, and Google
    // emits no map click at all — so a flag set in expectation of one is still armed when
    // the rider next clicks the map deliberately, and swallows that instead.
    const fake = createFakeMaps()
    const onMapClick = vi.fn()
    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        legs={[leg(coords(3))]}
        onMapClick={onMapClick}
        onLegGrab={() => true}
      />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    const map = fake.maps[0]

    act(() => {
      fake.polylines[0]?.mouseDown({ lat: 47.01, lon: -120 })
      map?.mouseMove({ lat: 47.01, lon: -120.2 })
      window.dispatchEvent(new MouseEvent('mouseup')) // released off the canvas
    })
    act(() => {
      map?.click({ lat: 49, lon: -121 })
    })

    expect(onMapClick).toHaveBeenCalledWith({ lat: 49, lon: -121 })
  })

  it('does not make the route grabbable when no handler was given', async () => {
    const fake = createFakeMaps()
    render(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(3))]} />)
    await waitFor(() => expect(fake.polylines).toHaveLength(1))

    expect(fake.polylines[0]?.listeners.filter((l) => !l.removed)).toHaveLength(0)
  })

  it('leaves no line listeners behind when the route is redrawn or unmounted', async () => {
    const { fake, view } = await dragging()
    const first = fake.polylines[0]

    view.unmount()

    expect(first?.listeners.every((listener) => listener.removed)).toBe(true)
  })
})

describe('MapCanvas', () => {
  it('says it is loading rather than showing an unexplained empty pane', () => {
    // A never-resolving loader stands in for a slow connection.
    render(<MapCanvas mapId={MAP_ID} loader={() => new Promise(() => undefined)} />)

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

  it('still shows pins when no Map ID is configured, and says why they look plain', async () => {
    // AdvancedMarkerElement needs a Map ID; without one it renders nothing and the only
    // signal is a console warning — a click reports "1 point placed" and no pin appears.
    // Falling back keeps the map usable, and the notice keeps the cause visible. This is
    // the configuration the app is actually in today, so it is not a hypothetical path.
    const fake = createFakeMaps()

    render(<MapCanvas loader={fake.loader} mapId="" waypoints={[waypoint(47)]} />)

    await waitFor(() => expect(fake.maps).toHaveLength(1))
    expect(fake.legacyMarkers).toHaveLength(1)
    expect(fake.markers).toHaveLength(0)
    expect(await screen.findByRole('status')).toHaveTextContent(/VITE_GOOGLE_MAPS_MAP_ID/)
  })

  it('asks for vector rendering only when a Map ID can support it', async () => {
    const withId = createFakeMaps()
    const withoutId = createFakeMaps()

    render(<MapCanvas loader={withId.loader} mapId={MAP_ID} />)
    await waitFor(() => expect(withId.maps).toHaveLength(1))
    render(<MapCanvas loader={withoutId.loader} mapId="" />)
    await waitFor(() => expect(withoutId.maps).toHaveLength(1))

    expect(withId.maps[0]?.options.renderingType).toBe('VECTOR')
    // Vector rendering requires a Map ID too, so asking for it without one is a request
    // Google cannot honour.
    expect(withoutId.maps[0]?.options.renderingType).toBeUndefined()
  })

  it('falls back rather than throwing when the marker library is absent', async () => {
    // If some other script loaded google.maps without the marker library, dereferencing
    // maps.marker.AdvancedMarkerElement throws inside an effect — and with no error
    // boundary above it, React unmounts the whole tree to a blank page.
    const fake = createFakeMaps({ withMarkerLibrary: false })

    render(<MapCanvas loader={fake.loader} mapId={MAP_ID} waypoints={[waypoint(47)]} />)

    await waitFor(() => expect(fake.legacyMarkers).toHaveLength(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('detaches fallback markers on unmount too', async () => {
    const fake = createFakeMaps()
    const { unmount } = render(<MapCanvas loader={fake.loader} mapId="" waypoints={[waypoint(47)]} />)
    await waitFor(() => expect(fake.legacyMarkers).toHaveLength(1))

    unmount()

    expect(fake.attached()).toEqual([])
  })

  it('shows why the map failed instead of a blank grey rectangle', async () => {
    const load = () => Promise.reject(new Error('No Google Maps browser key. Set VITE_...'))

    render(<MapCanvas mapId={MAP_ID} loader={load} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/No Google Maps browser key/)
  })

  it('draws one polyline per surface segment, in Google’s coordinate naming', async () => {
    const fake = createFakeMaps()

    render(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(3))]} />)

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
    const { rerender } = render(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(3))]} />)
    await waitFor(() => expect(fake.polylines).toHaveLength(1))

    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(4))]} />)

    await waitFor(() => expect(fake.polylines).toHaveLength(2))
    expect(fake.polylines[0]?.map).toBeNull()
    expect(fake.polylines[1]?.map).not.toBeNull()
  })

  it('rebuilds only the leg that changed, leaving its neighbours’ polylines alone', async () => {
    // Drag drives `legs` at the throttle interval. Rebuilding every polyline on each tick
    // means tearing down and recreating the whole route's geometry several times a second
    // to move one leg. Leg objects keep their identity when untouched — that is what
    // `insertVia` and `spliceRoutedLeg` guarantee — so identity is the signal.
    const fake = createFakeMaps()
    const untouched = leg(coords(3, 47))
    const { rerender } = render(
      <MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[untouched, leg(coords(3, 48))]} />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(2))
    const firstLegPolyline = fake.polylines[0]

    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[untouched, leg(coords(4, 48))]} />)

    await waitFor(() => expect(fake.polylines).toHaveLength(3))
    // The untouched leg keeps the very same overlay, still attached.
    expect(fake.polylines[0]).toBe(firstLegPolyline)
    expect(firstLegPolyline?.map).not.toBeNull()
    // The changed leg's old overlay is gone and a new one is up.
    expect(fake.polylines[1]?.map).toBeNull()
    expect(fake.polylines[2]?.map).not.toBeNull()
  })

  it('does not redraw a leg whose indices shifted but whose geometry did not', async () => {
    // Inserting a via-point renumbers every leg after it, so `insertVia` returns a new
    // TripLeg object for each — same geometry, shifted indices. Keying on the leg object
    // therefore rebuilt the whole route on every throttle tick of a drag: a six-leg route
    // dragged for five seconds is thirty Polyline constructions where five would do.
    const fake = createFakeMaps()
    const geometry = leg(coords(3, 48))
    const { rerender } = render(
      <MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(3, 47)), geometry]} />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(2))
    const secondLegPolyline = fake.polylines[1]

    // A renumbered leg: new object, same routed geometry, exactly as insertVia produces.
    const shifted = { ...geometry, start_waypoint_index: 2, end_waypoint_index: 3 }
    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(4, 47)), shifted]} />)

    await waitFor(() => expect(fake.polylines.length).toBeGreaterThan(2))
    expect(fake.polylines[1]).toBe(secondLegPolyline)
    expect(secondLegPolyline?.map).not.toBeNull()
  })

  it('detaches a leg’s polylines when the leg is removed entirely', async () => {
    const fake = createFakeMaps()
    const kept = leg(coords(3, 47))
    const { rerender } = render(
      <MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[kept, leg(coords(3, 48))]} />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(2))

    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[kept]} />)

    await waitFor(() => expect(fake.polylines[1]?.map).toBeNull())
    expect(fake.polylines[0]?.map).not.toBeNull()
  })

  it('never rebuilds the map when props change', async () => {
    // Recreating it would reset zoom and centre while the user is looking at them.
    const fake = createFakeMaps()
    const { rerender } = render(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[]} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))

    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(3))]} />)
    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(5))]} zoom={12} />)

    await waitFor(() => expect(fake.polylines.length).toBeGreaterThan(1))
    expect(fake.maps).toHaveLength(1)
  })

  it('marks every waypoint, labelling the ends differently from the middle', async () => {
    const fake = createFakeMaps()

    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        waypoints={[waypoint(47), waypoint(48), waypoint(49, 'Sun Mountain Lodge')]}
      />,
    )

    await waitFor(() => expect(fake.markers).toHaveLength(3))

    // Resolved by role and computed accessible name rather than by reading aria-label
    // back: an attribute the browser ignores would satisfy the latter and announce nothing.
    const pins = fake.markers.map((marker) => marker.options['content'] as HTMLElement)
    document.body.append(...pins)
    const byName = ['Start', 'Via point', 'End: Sun Mountain Lodge'].map((name) =>
      screen.getByRole('img', { name }),
    )
    expect(byName).toEqual(pins) // same elements, in route order
  })

  it('reports a map click as a domain coordinate — the mouse path for setting points', async () => {
    // Chat is an accelerator, never a requirement: setting start and end with the mouse
    // has to work on its own.
    const fake = createFakeMaps()
    const onMapClick = vi.fn()

    render(<MapCanvas mapId={MAP_ID} loader={fake.loader} onMapClick={onMapClick} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))
    fake.maps[0]?.click({ lat: 47.5, lon: -120.25 })

    expect(onMapClick).toHaveBeenCalledWith({ lat: 47.5, lon: -120.25 })
  })

  it('keeps calling the latest click handler without rebinding the map listener', async () => {
    const fake = createFakeMaps()
    const first = vi.fn()
    const second = vi.fn()
    const { rerender } = render(<MapCanvas mapId={MAP_ID} loader={fake.loader} onMapClick={first} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))

    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} onMapClick={second} />)
    fake.maps[0]?.click({ lat: 1, lon: 2 })

    expect(second).toHaveBeenCalledWith({ lat: 1, lon: 2 })
    expect(first).not.toHaveBeenCalled()
    expect(fake.maps[0]?.listeners.filter((l) => l.event === 'click')).toHaveLength(1)
  })

  it('leaves nothing attached to the map after unmount', async () => {
    const fake = createFakeMaps()
    const { unmount } = render(
      <MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(3))]} waypoints={[waypoint(47)]} />,
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

    const { unmount } = render(<MapCanvas mapId={MAP_ID} loader={load} />)
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

    const { rerender } = render(<MapCanvas mapId={MAP_ID} loader={stalled} />)
    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} />)
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
    const { rerender } = render(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[]} />)
    await waitFor(() => expect(fake.maps).toHaveLength(1))
    expect(fake.maps[0]?.fitted).toHaveLength(0)

    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(3))]} />)
    await waitFor(() => expect(fake.maps[0]?.fitted).toHaveLength(1))
    expect(fake.maps[0]?.fitted[0]?.extended).toHaveLength(3)

    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} legs={[leg(coords(6))]} />)
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

    render(<MapCanvas mapId={MAP_ID} loader={load} />)
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: /try again/i }))

    await waitFor(() => expect(fake.maps).toHaveLength(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
