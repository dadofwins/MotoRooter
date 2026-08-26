import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MapCanvas } from './MapCanvas'
import { FAN_MAX_MEMBERS } from './fan'
import type { GoogleMaps } from './loadGoogleMaps'
import { poi as placeFixture, routeLeg } from '../api/fixtures'
import type { Coordinate, Poi, TripLeg, Waypoint } from '../api/types'

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

/** Where a Maps event happened on screen. The API carries this on `domEvent`. */
interface ScreenAt {
  readonly x: number
  readonly y: number
}

/** The browser's own menu, which has to be suppressed or it lands on top of ours. */
const nativeMenu = { preventDefault: vi.fn() }

/** Delivers a Maps event to every live listener registered for it. */
function emit(
  listeners: readonly FakeListener[],
  event: string,
  coordinate: Coordinate,
  at?: ScreenAt,
): void {
  for (const listener of listeners.filter((l) => l.event === event && !l.removed)) {
    listener.handler({
      latLng: { lat: () => coordinate.lat, lng: () => coordinate.lon },
      ...(at === undefined
        ? {}
        : { domEvent: { clientX: at.x, clientY: at.y, preventDefault: nativeMenu.preventDefault } }),
    })
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
    /** Drives a zoom the way the rider would: the value changes, then the map says so. */
    zoomTo(zoom: number): void {
      this.#zoom = zoom
      for (const listener of this.listeners.filter((l) => l.event === 'zoom_changed' && !l.removed)) {
        listener.handler({})
      }
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
    /** Drives a right-click on the line, which carries a screen position with it. */
    rightClick(coordinate: Coordinate, at: ScreenAt = { x: 0, y: 0 }): void {
      emit(this.listeners, 'contextmenu', coordinate, at)
    }
  }

  class FakeMarker {
    map: unknown
    position: unknown
    readonly listeners = new Map<string, (event: unknown) => void>()
    constructor(readonly options: Record<string, unknown>) {
      this.map = options['map'] ?? null
      this.position = options['position'] ?? null
      markers.push(this)
    }
    addListener(event: string, handler: (event: unknown) => void): { remove: () => void } {
      this.listeners.set(event, handler)
      return { remove: () => this.listeners.delete(event) }
    }
    click(at?: ScreenAt): void {
      this.listeners.get('click')?.(
        at === undefined
          ? {}
          : { domEvent: { clientX: at.x, clientY: at.y, preventDefault: nativeMenu.preventDefault } },
      )
    }
    contextMenu(at?: ScreenAt): void {
      this.listeners.get('contextmenu')?.(
        at === undefined
          ? {}
          : {
              domEvent: {
                clientX: at.x,
                clientY: at.y,
                preventDefault: nativeMenu.preventDefault,
              },
            },
      )
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
    readonly listeners = new Map<string, (event: unknown) => void>()
    addListener(event: string, handler: (event: unknown) => void): { remove: () => void } {
      this.listeners.set(event, handler)
      return { remove: () => this.listeners.delete(event) }
    }
    click(at?: ScreenAt): void {
      this.listeners.get('click')?.(
        at === undefined
          ? {}
          : { domEvent: { clientX: at.x, clientY: at.y, preventDefault: nativeMenu.preventDefault } },
      )
    }
    contextMenu(at?: ScreenAt): void {
      this.listeners.get('contextmenu')?.(
        at === undefined
          ? {}
          : {
              domEvent: {
                clientX: at.x,
                clientY: at.y,
                preventDefault: nativeMenu.preventDefault,
              },
            },
      )
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
    /** Markers whose content is a POI pin. */
    poiMarkers: () =>
      [...markers, ...legacyMarkers].filter(
        (marker) =>
          marker.map !== null &&
          (marker.options['content'] as HTMLElement | undefined)?.className?.startsWith('poi ') ===
            true,
      ),
    poiPins: () =>
      [...markers, ...legacyMarkers]
        .filter((marker) => marker.map !== null)
        .map((marker) => marker.options['content'] as HTMLElement | undefined)
        .filter((pin) => pin?.className?.startsWith('poi ') === true),
    /** Markers standing for a group of places, which show a count rather than a glyph. */
    clusterMarkers: () =>
      [...markers, ...legacyMarkers].filter(
        (marker) =>
          marker.map !== null &&
          (marker.options['content'] as HTMLElement | undefined)?.className?.startsWith(
            'poi-cluster',
          ) === true,
      ),
    clusterPins: () =>
      [...markers, ...legacyMarkers]
        .filter((marker) => marker.map !== null)
        .map((marker) => marker.options['content'] as HTMLElement | undefined)
        .filter((pin) => pin?.className?.startsWith('poi-cluster') === true),
    /** Markers standing for a trip waypoint, in the order they were placed. */
    waypointMarkers: () =>
      [...markers, ...legacyMarkers].filter(
        (marker) =>
          marker.map !== null &&
          (marker.options['content'] as HTMLElement | undefined)?.className?.startsWith('pin ') ===
            true,
      ),
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
    routed: routeLeg({ geometry: [...geometry], distance_m: 1, duration_s: 1 }),
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

    // Only the press matters here. The line always listens for a right-click, because that
    // listener is cached per leg and cannot be added retroactively.
    expect(
      fake.polylines[0]?.listeners.filter((l) => !l.removed && l.event === 'mousedown'),
    ).toHaveLength(0)
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

/**
 * Points of interest on the map.
 *
 * Right-click is the mouse path for putting a place on the route — the same action the
 * assistant will take, because chat is an accelerator and never the only way in.
 */
describe('MapCanvas showing points of interest', () => {
  function poi(overrides: Partial<Poi> = {}): Poi {
    return {
      id: 'poi-1',
      name: 'Lone Fir Campground',
      category: 'campground',
      coordinate: { lat: 47.5, lon: -120.1 },
      source: 'places',
      place_id: 'ChIJ123',
      note: null,
      on_route: false,
      ...overrides,
    }
  }

  it('draws a pin for each place', async () => {
    const fake = createFakeMaps()

    // At a planning zoom. Fifty kilometres apart is nine pixels at the opening camera, which
    // is one pin's worth — those two are a crowd, and there is a test below that says so.
    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        zoom={12}
        pois={[poi(), poi({ id: 'poi-2', category: 'fuel', coordinate: { lat: 48, lon: -120 } })]}
      />,
    )

    await waitFor(() => expect(fake.poiPins()).toHaveLength(2))
  })

  it('reports a plain click on a place, which opens its detail', async () => {
    const fake = createFakeMaps()
    const onPoiOpen = vi.fn()
    const place = poi()

    render(<MapCanvas mapId={MAP_ID} loader={fake.loader} pois={[place]} onPoiOpen={onPoiOpen} />)
    await waitFor(() => expect(fake.poiPins()).toHaveLength(1))

    act(() => {
      fake.poiMarkers()[0]?.click()
    })

    expect(onPoiOpen).toHaveBeenCalledWith(place)
  })

  it('does not offer to route to an unconfirmed suggestion', async () => {
    // The backend refuses to pin one, so offering it would be a control that cannot work.
    // It still opens — a rider may want to read what was suggested and why.
    const fake = createFakeMaps()
    const onContextMenu = vi.fn()
    const onPoiOpen = vi.fn()
    const guess = poi({ source: 'llm_suggested', place_id: null })

    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        pois={[guess]}
        onContextMenu={onContextMenu}
        onPoiOpen={onPoiOpen}
      />,
    )
    await waitFor(() => expect(fake.poiPins()).toHaveLength(1))

    act(() => {
      fake.poiMarkers()[0]?.contextMenu()
      fake.poiMarkers()[0]?.click()
    })

    expect(onContextMenu).not.toHaveBeenCalled()
    expect(onPoiOpen).toHaveBeenCalledWith(guess)
  })

  it('replaces its pins when the places change, leaving none attached', async () => {
    const fake = createFakeMaps()
    const { rerender } = render(<MapCanvas mapId={MAP_ID} loader={fake.loader} pois={[poi()]} />)
    await waitFor(() => expect(fake.poiPins()).toHaveLength(1))

    rerender(<MapCanvas mapId={MAP_ID} loader={fake.loader} pois={[]} />)

    await waitFor(() => expect(fake.poiPins()).toHaveLength(0))
  })

  it('detaches its pins on unmount', async () => {
    const fake = createFakeMaps()
    const { unmount } = render(<MapCanvas mapId={MAP_ID} loader={fake.loader} pois={[poi()]} />)
    await waitFor(() => expect(fake.poiPins()).toHaveLength(1))

    unmount()

    expect(fake.poiPins()).toHaveLength(0)
  })
})

/**
 * Right-click, reported rather than acted on.
 *
 * Right-click already *did* things here — removed a waypoint, added a place to the route — with
 * no label and no confirmation, so the rider who discovered it discovered it by doing it. The
 * canvas now says what was clicked and where on screen; the caller opens a named menu over it.
 *
 * The screen position is the part with no obvious test elsewhere: a menu that opens in the
 * corner instead of under the cursor is a menu about the wrong thing.
 */
describe('MapCanvas reporting a right-click', () => {
  it('reports a right-clicked point with where on screen it happened', async () => {
    const fake = createFakeMaps()
    const onContextMenu = vi.fn()

    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        waypoints={[waypoint(47), waypoint(47.02)]}
        onContextMenu={onContextMenu}
      />,
    )
    await waitFor(() => expect(fake.waypointMarkers()).toHaveLength(2))

    act(() => {
      fake.waypointMarkers()[1]?.contextMenu({ x: 340, y: 210 })
    })

    expect(onContextMenu).toHaveBeenCalledWith({
      kind: 'waypoint',
      index: 1,
      at: { x: 340, y: 210 },
    })
  })

  it('reports a right-clicked place rather than adding it outright', async () => {
    // The silent add was the discoverability problem in the other direction: nothing said it had
    // happened, or that it could.
    const fake = createFakeMaps()
    const onContextMenu = vi.fn()
    const place = placeFixture({ source: 'places', place_id: 'ChIJ123' })

    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        pois={[place]}
        onContextMenu={onContextMenu}
      />,
    )
    await waitFor(() => expect(fake.poiPins()).toHaveLength(1))

    act(() => {
      fake.poiMarkers()[0]?.contextMenu({ x: 80, y: 90 })
    })

    expect(onContextMenu).toHaveBeenCalledWith({ kind: 'poi', poi: place, at: { x: 80, y: 90 } })
  })

  it('reports a right-click on the line with the leg and the point on it', async () => {
    // Both are load-bearing for "add point here": the coordinate is where the new point goes,
    // and the leg is the one being split in two.
    const fake = createFakeMaps()
    const onContextMenu = vi.fn()

    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        waypoints={[waypoint(47), waypoint(47.02)]}
        legs={[leg(coords(3, 47)), leg(coords(3, 48))]}
        onContextMenu={onContextMenu}
      />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(2))

    act(() => {
      fake.polylines[1]?.rightClick({ lat: 48.01, lon: -120 }, { x: 500, y: 400 })
    })

    expect(onContextMenu).toHaveBeenCalledWith({
      kind: 'route',
      legIndex: 1,
      coordinate: { lat: 48.01, lon: -120 },
      at: { x: 500, y: 400 },
    })
  })

  it('keeps the browser out of the way, so one menu opens rather than two', async () => {
    // Two menus stacked is worse than the unlabelled right-click this replaces: the rider picks
    // from whichever won the paint.
    const fake = createFakeMaps()
    nativeMenu.preventDefault.mockClear()

    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        waypoints={[waypoint(47), waypoint(47.02)]}
        onContextMenu={vi.fn()}
      />,
    )
    await waitFor(() => expect(fake.waypointMarkers()).toHaveLength(2))

    act(() => {
      fake.waypointMarkers()[0]?.contextMenu({ x: 10, y: 10 })
    })

    expect(nativeMenu.preventDefault).toHaveBeenCalled()
  })

  it('leaves no line listener behind when the route is redrawn', async () => {
    // Every listener added per leg is one more to leak on a route that gets edited all evening.
    const fake = createFakeMaps()
    const view = render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        waypoints={[waypoint(47), waypoint(47.02)]}
        legs={[leg(coords(3, 47))]}
        onContextMenu={vi.fn()}
      />,
    )
    await waitFor(() => expect(fake.polylines).toHaveLength(1))
    const first = fake.polylines[0]

    view.rerender(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        waypoints={[waypoint(47), waypoint(47.02)]}
        legs={[leg(coords(4, 47.5))]}
        onContextMenu={vi.fn()}
      />,
    )

    await waitFor(() => expect(fake.polylines.length).toBeGreaterThan(1))
    expect(first?.listeners.every((listener) => listener.removed)).toBe(true)
  })
})

/**
 * Pins that would land on top of each other.
 *
 * Measured on a live corridor: at the zoom where a rider sees the whole day, 29 of 31 pins were
 * obscured by another, and the visible one was whichever was drawn last. So the map was silently
 * under-reporting what discovery found, and there was no way to reach the places underneath.
 *
 * The grouping arithmetic is tested in cluster.test.ts. What is tested here is that the canvas
 * asks the question at the right zoom, asks it again when the rider changes the zoom, and leaves
 * nothing attached behind.
 */
describe('MapCanvas clustering places', () => {
  /**
   * Two places about 250 m apart — nine pixels at a planning zoom, seventy at zoom 15.
   *
   * A real spacing rather than a contrived one: campgrounds off the same forest road sit about
   * this far from each other, which is exactly why the live corridor had 29 of 31 pins covered.
   */
  const CROWDED = [
    placeFixture({ id: 'a', name: 'Lone Fir', coordinate: { lat: 47.5, lon: -120.5 } }),
    placeFixture({ id: 'b', name: 'Mineral Springs', coordinate: { lat: 47.5015, lon: -120.4985 } }),
  ]

  it('draws one pin for a crowd rather than a pile of pins', async () => {
    const fake = createFakeMaps()

    render(<MapCanvas mapId={MAP_ID} loader={fake.loader} zoom={12} pois={CROWDED} />)

    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))
    expect(fake.clusterPins()[0]?.textContent).toBe('2')
    // And not the places themselves, or the cluster would sit on top of what it stands for.
    expect(fake.poiPins()).toHaveLength(0)
  })

  it('leaves places that do not collide as themselves', async () => {
    const fake = createFakeMaps()
    const apart = [
      placeFixture({ id: 'a', coordinate: { lat: 47.5, lon: -120.5 } }),
      placeFixture({ id: 'b', coordinate: { lat: 47.9, lon: -120.1 } }),
    ]

    render(<MapCanvas mapId={MAP_ID} loader={fake.loader} zoom={12} pois={apart} />)

    await waitFor(() => expect(fake.poiPins()).toHaveLength(2))
    expect(fake.clusterPins()).toHaveLength(0)
  })

  it('takes the crowd apart when the rider zooms in', async () => {
    // Zooming is the rider's own way of resolving a cluster, and it has to actually work —
    // otherwise the only way in is the disclosure, and a map that never separates is a list.
    const fake = createFakeMaps()
    render(<MapCanvas mapId={MAP_ID} loader={fake.loader} zoom={12} pois={CROWDED} />)
    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))

    act(() => {
      fake.maps[0]?.zoomTo(15)
    })

    await waitFor(() => expect(fake.poiPins()).toHaveLength(2))
    expect(fake.clusterPins()).toHaveLength(0)
  })

  it('reports a crowd too big to fan with what is in it and where it is', async () => {
    // Past the fan's ceiling the caller opens a list, so the canvas hands over the members and
    // the point to open it at. Below the ceiling it opens the group on the map itself.
    const fake = createFakeMaps()
    const onClusterOpen = vi.fn()
    const many = Array.from({ length: 9 }, (_, index) =>
      placeFixture({
        id: `p${String(index)}`,
        coordinate: { lat: 47.5 + index * 0.0004, lon: -120.5 + index * 0.0004 },
      }),
    )

    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        zoom={12}
        pois={many}
        onClusterOpen={onClusterOpen}
      />,
    )
    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))

    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 210, y: 130 })
    })

    expect(onClusterOpen).toHaveBeenCalledWith({ members: many, at: { x: 210, y: 130 } })
  })

  it('opens a lone place directly rather than through a crowd of one', async () => {
    const fake = createFakeMaps()
    const onPoiOpen = vi.fn()
    const onClusterOpen = vi.fn()
    const only = placeFixture({ id: 'a', coordinate: { lat: 47.5, lon: -120.5 } })

    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        pois={[only]}
        onPoiOpen={onPoiOpen}
        onClusterOpen={onClusterOpen}
      />,
    )
    await waitFor(() => expect(fake.poiPins()).toHaveLength(1))

    act(() => {
      fake.poiMarkers()[0]?.click()
    })

    expect(onPoiOpen).toHaveBeenCalledWith(only)
    expect(onClusterOpen).not.toHaveBeenCalled()
  })

  it('leaves nothing attached when the places go away', async () => {
    const fake = createFakeMaps()
    const view = render(<MapCanvas mapId={MAP_ID} loader={fake.loader} zoom={12} pois={CROWDED} />)
    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))

    view.unmount()

    expect(fake.attached()).toHaveLength(0)
  })
})

/**
 * Opening a small group out into its members.
 *
 * Tim's call after the measurement: fan up to eight, list beyond that. A fan keeps the places on
 * the map instead of moving them into a panel, and it only fits while the pins have room to be
 * pins — twelve on a fixed radius sit 21px apart, which is narrower than a pin.
 *
 * A fanned pin is not where its place is. The leader line back to the group is what makes that a
 * disclosed offset rather than a lie, so it is tested as hard as the pins are.
 */
describe('MapCanvas fanning a small group', () => {
  /** Places about 250 m apart, which is one pin's width at a planning zoom. */
  function crowd(count: number): Poi[] {
    return Array.from({ length: count }, (_, index) =>
      placeFixture({
        id: `p${String(index)}`,
        name: `place ${String(index)}`,
        coordinate: { lat: 47.5 + index * 0.0004, lon: -120.5 + index * 0.0004 },
      }),
    )
  }

  async function withCrowd(count: number, onClusterOpen = vi.fn()) {
    const fake = createFakeMaps()
    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        zoom={12}
        pois={crowd(count)}
        onClusterOpen={onClusterOpen}
      />,
    )
    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))
    return { fake, onClusterOpen }
  }

  it('opens a pair into its two places', async () => {
    const { fake } = await withCrowd(2)

    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })

    await waitFor(() => expect(fake.poiPins()).toHaveLength(2))
  })

  it('draws a line from the group to each place it moved', async () => {
    // The pins are not where their places are. Without the line that is a lie; with it, it is an
    // offset the rider can see and follow back.
    const { fake } = await withCrowd(3)
    const before = fake.polylines.filter((line) => line.map !== null).length

    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })

    await waitFor(() => {
      expect(fake.polylines.filter((line) => line.map !== null).length).toBe(before + 3)
    })
  })

  it('hands a big group to the caller instead, because a fan of nine overlaps itself', async () => {
    const onClusterOpen = vi.fn()
    const { fake } = await withCrowd(9, onClusterOpen)

    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })

    expect(onClusterOpen).toHaveBeenCalled()
    expect(fake.poiPins()).toHaveLength(0)
  })

  it('fans the largest group it will fan, and lists the next one up', async () => {
    // The threshold itself, tested at both sides of it rather than at a number written here.
    const fanned = await withCrowd(FAN_MAX_MEMBERS)
    act(() => {
      fanned.fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })
    await waitFor(() => expect(fanned.fake.poiPins()).toHaveLength(FAN_MAX_MEMBERS))
    expect(fanned.onClusterOpen).not.toHaveBeenCalled()

    const listed = await withCrowd(FAN_MAX_MEMBERS + 1)
    act(() => {
      listed.fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })
    expect(listed.onClusterOpen).toHaveBeenCalled()
  })

  it('opens a fanned place when it is clicked, the same as any other pin', async () => {
    const fake = createFakeMaps()
    const onPoiOpen = vi.fn()
    const places = crowd(2)
    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        zoom={12}
        pois={places}
        onPoiOpen={onPoiOpen}
      />,
    )
    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))
    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })
    await waitFor(() => expect(fake.poiMarkers()).toHaveLength(2))

    act(() => {
      fake.poiMarkers()[0]?.click()
    })

    expect(onPoiOpen).toHaveBeenCalledWith(places[0])
  })

  it('gives a fanned place its right-click, so adding one to the route still works', async () => {
    // The regression clustering introduced: every place in a group lost the add-to-route idiom,
    // because the group's own pin was the only thing left to click.
    const fake = createFakeMaps()
    const onContextMenu = vi.fn()
    const places = crowd(2)
    render(
      <MapCanvas
        mapId={MAP_ID}
        loader={fake.loader}
        zoom={12}
        pois={places}
        onContextMenu={onContextMenu}
      />,
    )
    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))
    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })
    await waitFor(() => expect(fake.poiMarkers()).toHaveLength(2))

    act(() => {
      fake.poiMarkers()[1]?.contextMenu({ x: 12, y: 34 })
    })

    expect(onContextMenu).toHaveBeenCalledWith({
      kind: 'poi',
      poi: places[1],
      at: { x: 12, y: 34 },
    })
  })

  it('keeps the group pin as the hub the lines run back to', async () => {
    // Found by rendering it: the fan first shipped drawing only the members, so the leader lines
    // converged on nothing and there was no way to close what had been opened. A picture drawn
    // by hand had a hub in it; the code did not.
    const { fake } = await withCrowd(3)

    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })

    await waitFor(() => expect(fake.poiPins()).toHaveLength(3))
    expect(fake.clusterPins()).toHaveLength(1)
  })

  it('closes the fan when the rider clicks the group again', async () => {
    // The obvious affordance: the thing that opened it closes it. Without this the only way out
    // is a click on the map, which is a different gesture in a different place.
    const { fake } = await withCrowd(3)
    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })
    await waitFor(() => expect(fake.poiPins()).toHaveLength(3))

    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })

    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))
    expect(fake.poiPins()).toHaveLength(0)
  })

  it('closes the fan again when the rider clicks the map', async () => {
    const { fake } = await withCrowd(3)
    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })
    await waitFor(() => expect(fake.poiPins()).toHaveLength(3))

    act(() => {
      fake.maps[0]?.click({ lat: 47.2, lon: -120.9 })
    })

    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))
    expect(fake.poiPins()).toHaveLength(0)
  })

  it('closes the fan when the rider zooms, because the group may not exist any more', async () => {
    const { fake } = await withCrowd(3)
    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })
    await waitFor(() => expect(fake.poiPins()).toHaveLength(3))

    act(() => {
      fake.maps[0]?.zoomTo(16)
    })

    // Separated by the zoom rather than by the fan, and no leader lines left pointing at a group
    // that is no longer there.
    await waitFor(() => expect(fake.polylines.filter((line) => line.map !== null)).toHaveLength(0))
  })

  it('leaves nothing attached when a fan is open at unmount', async () => {
    const fake = createFakeMaps()
    const view = render(
      <MapCanvas mapId={MAP_ID} loader={fake.loader} zoom={12} pois={crowd(4)} />,
    )
    await waitFor(() => expect(fake.clusterPins()).toHaveLength(1))
    act(() => {
      fake.clusterMarkers()[0]?.click({ x: 100, y: 100 })
    })
    await waitFor(() => expect(fake.poiPins()).toHaveLength(4))

    view.unmount()

    expect(fake.attached()).toHaveLength(0)
  })
})
