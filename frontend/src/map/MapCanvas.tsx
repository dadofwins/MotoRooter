/**
 * The map canvas.
 *
 * Everything the assistant can do must also be doable with the mouse, and this is where the
 * mouse path starts: clicking the map reports a coordinate, which is how a user sets a
 * start and end point without typing a word.
 *
 * Two lifecycle rules drive the shape of this component:
 *
 * - **The map is built once and never rebuilt.** Recreating it on a prop change would reset
 *   zoom and centre while the user is looking at them. `center` and `zoom` are therefore
 *   the *initial* camera only.
 * - **Every overlay is detached when it is replaced.** Each sync effect returns a cleanup
 *   that removes exactly the overlays it created, so a long editing session cannot
 *   accumulate orphaned polylines — the failure mode that quietly turns a map to treacle.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Coordinate, Poi, TripLeg, Waypoint } from '../api/types'
import type { GoogleMaps, GoogleMapsLoader } from './loadGoogleMaps'
import { distanceM } from '../routing/geo'
import { createMapOptions, toCoordinate, toLatLng, type MapColorScheme } from './mapOptions'
import { polylineStyle, toRouteSegments } from './routeLayer'
import { createPoiPin, isVerified } from './poiPin'
import { createDragHandle, createWaypointPin, waypointKind } from './waypointPin'

/** Stable identities: inline defaults would rebuild every overlay on every render. */
const NO_LEGS: readonly TripLeg[] = []
const NO_WAYPOINTS: readonly Waypoint[] = []
const NO_POIS: readonly Poi[] = []

/** Opening view: BDR country, wide enough to place a first point anywhere in it. */
const DEFAULT_CENTER: Coordinate = { lat: 44.5, lon: -116.5 }
const DEFAULT_ZOOM = 5

const NO_MAP_ID_NOTICE =
  'Waypoints are shown as plain markers: set VITE_GOOGLE_MAPS_MAP_ID in frontend/.env.local ' +
  'to a vector Map ID for styled pins and vector rendering.'

/** A marker of either generation, reduced to the operations the canvas needs. */
interface AttachedMarker {
  readonly detach: () => void
  readonly move: (position: google.maps.LatLngLiteral) => void
  readonly on: (
    event: string,
    handler: (event: unknown) => void,
  ) => google.maps.MapsEventListener | null
}

interface MarkerInput {
  readonly map: google.maps.Map
  readonly position: google.maps.LatLngLiteral
  readonly pin: HTMLElement
  /** Advanced markers are richer but need a Map ID; plain ones work anywhere. */
  readonly advanced: boolean
}

/**
 * Builds a waypoint marker, preferring the advanced one.
 *
 * The library check is not defensive padding: `alreadyLoaded()` can pick up a `google.maps`
 * that some other script loaded without the `marker` library, and dereferencing
 * `maps.marker.AdvancedMarkerElement` then throws inside an effect. With no error boundary
 * above it, React unmounts the whole tree — the map, the chat rail, everything — to a blank
 * page, because a pin could not be drawn.
 */
function createMarker(maps: GoogleMaps, input: MarkerInput): AttachedMarker {
  const Advanced = maps.marker?.AdvancedMarkerElement
  if (input.advanced && Advanced !== undefined) {
    const marker = new Advanced({
      map: input.map,
      position: input.position,
      content: input.pin,
      title: input.pin.title,
    })
    return {
      detach: () => {
        marker.map = null
      },
      move: (position) => {
        marker.position = position
      },
      on: (event, handler) => marker.addListener(event, handler),
    }
  }

  // Deprecated, but it renders without a Map ID, which is the whole point here.
  const marker = new maps.Marker({
    map: input.map,
    position: input.position,
    title: input.pin.title,
    label: input.pin.textContent ?? undefined,
  })
  return {
    detach: () => {
      marker.setMap(null)
    },
    move: (position) => {
      marker.setPosition(position)
    },
    on: (event, handler) => marker.addListener(event, handler),
  }
}

/** What was right-clicked, and where on screen, so a menu can open over it. */
export type ContextTarget =
  | { readonly kind: 'waypoint'; readonly index: number; readonly at: ScreenPoint }
  | { readonly kind: 'poi'; readonly poi: Poi; readonly at: ScreenPoint }
  | {
      readonly kind: 'route'
      readonly legIndex: number
      readonly coordinate: Coordinate
      readonly at: ScreenPoint
    }

/** A position in viewport pixels, which is what a menu needs to open under the cursor. */
export interface ScreenPoint {
  readonly x: number
  readonly y: number
}

/**
 * Where a right-click happened on screen, and the browser told to stay out of it.
 *
 * Both halves belong together: the only reason to read the position is to open our own menu
 * there, and two menus stacked is worse than the unlabelled right-click this replaces — the
 * rider picks from whichever won the paint.
 */
function contextPointOf(event: unknown): ScreenPoint {
  const dom = (
    event as {
      domEvent?: { clientX?: number; clientY?: number; preventDefault?: () => void }
    }
  ).domEvent
  dom?.preventDefault?.()
  return { x: dom?.clientX ?? 0, y: dom?.clientY ?? 0 }
}

export interface MapCanvasProps {
  /**
   * Resolves the Maps API. Must be a stable reference — create it once at module scope,
   * not inline in a render.
   */
  readonly loader: GoogleMapsLoader
  readonly waypoints?: readonly Waypoint[]
  readonly legs?: readonly TripLeg[]
  readonly pois?: readonly Poi[]
  /** Initial camera only; later changes do not move a map the user may be panning. */
  readonly center?: Coordinate
  readonly zoom?: number
  readonly mapId?: string
  readonly colorScheme?: MapColorScheme
  /** A click on the basemap, in domain coordinates. The mouse path for setting points. */
  readonly onMapClick?: (coordinate: Coordinate) => void

  /**
   * The route line was pressed. Return whether a drag should start — a leg with no geometry
   * cannot be dragged, and refusing here keeps the map pannable.
   */
  readonly onLegGrab?: (legIndex: number, at: Coordinate) => boolean
  /** The pointer moved during a drag. Throttling belongs to the caller, not here. */
  readonly onLegDrag?: (at: Coordinate) => void
  /** The drag ended. Always fires once a drag has started, wherever the button came up. */
  readonly onLegDrop?: (at: Coordinate) => void
  /** The gesture ended without moving far enough to be a drag. Abandon it. */
  readonly onLegCancel?: () => void

  /**
   * Something was right-clicked, with where on screen it happened.
   *
   * Reported rather than acted on, which is the whole point. Right-click used to remove a
   * waypoint and add a place to the route directly, with no label and no confirmation — the worst
   * kind of destructive action, because the rider who discovers it discovers it by doing it. The
   * caller turns this into a named menu.
   *
   * Never fires on an unconfirmed suggestion's pin: the backend refuses to pin one to the route,
   * so there is nothing to offer.
   */
  readonly onContextMenu?: (target: ContextTarget) => void
  /** A place was clicked. Opens its detail, whatever its provenance. */
  readonly onPoiOpen?: (poi: Poi) => void
}

/**
 * How far the pointer must travel before a press counts as a drag.
 *
 * In screen pixels rather than metres: at zoom 18 a few pixels is a couple of metres and at
 * zoom 8 it is a kilometre, so a distance threshold would swallow real drags when zoomed in
 * and invent them when zoomed out.
 */
const DRAG_THRESHOLD_PX = 5

/** Ground distance one screen pixel covers, at this latitude and zoom. */
function metresPerPixel(latitude: number, zoom: number): number {
  return (156_543.033_92 * Math.cos((latitude * Math.PI) / 180)) / 2 ** zoom
}

export function MapCanvas({
  loader,
  waypoints = NO_WAYPOINTS,
  legs = NO_LEGS,
  pois = NO_POIS,
  center = DEFAULT_CENTER,
  zoom = DEFAULT_ZOOM,
  mapId,
  colorScheme,
  onMapClick,
  onLegGrab,
  onLegDrag,
  onLegDrop,
  onLegCancel,
  onContextMenu,
  onPoiOpen,
}: MapCanvasProps): React.JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<google.maps.Map | null>(null)
  const hasFramedRef = useRef(false)
  const onMapClickRef = useRef(onMapClick)

  const [maps, setMaps] = useState<GoogleMaps | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [attempt, setAttempt] = useState(0)

  /**
   * Whether a vector Map ID is configured.
   *
   * Both vector rendering and `AdvancedMarkerElement` require one. Without it the basemap
   * still draws, so the app *looks* fine — and then a click reports "1 point placed" while
   * no pin ever appears, with only a console warning to explain it.
   *
   * The map is not blocked over it: a working map with plain markers beats an error box,
   * and this is the configuration the app ships in today. But the degradation is stated on
   * screen rather than left to the console.
   */
  const hasMapId = (mapId ?? '') !== ''

  // Read at event time rather than captured, so a new handler does not mean a new listener.
  useEffect(() => {
    onMapClickRef.current = onMapClick
  }, [onMapClick])

  // Explicit `| undefined` on each: exactOptionalPropertyTypes distinguishes "absent" from
  // "present and undefined", and these are assigned wholesale on every change.
  const dragHandlers = useRef<{
    onLegGrab?: ((legIndex: number, at: Coordinate) => boolean) | undefined
    onLegDrag?: ((at: Coordinate) => void) | undefined
    onLegDrop?: ((at: Coordinate) => void) | undefined
    onLegCancel?: (() => void) | undefined
  }>({})
  useEffect(() => {
    dragHandlers.current = { onLegGrab, onLegDrag, onLegDrop, onLegCancel }
  }, [onLegGrab, onLegDrag, onLegDrop, onLegCancel])

  const poiHandlers = useRef<{
    onPoiOpen?: ((poi: Poi) => void) | undefined
  }>({})
  useEffect(() => {
    poiHandlers.current = { onPoiOpen }
  }, [onPoiOpen])

  const contextHandler = useRef(onContextMenu)
  useEffect(() => {
    contextHandler.current = onContextMenu
  }, [onContextMenu])

  /**
   * The gesture in progress, if any.
   *
   * A ref, and imperative overlays, deliberately. The handle has to follow the pointer at
   * frame rate, and putting the cursor position through the state that also feeds routing
   * is what made the drag session rebuild itself mid-gesture. Nothing here re-renders.
   */
  const gesture = useRef<{
    from: Coordinate
    last: Coordinate
    handle: AttachedMarker | null
  } | null>(null)
  /**
   * Google emits a click after the mouseup that ended a drag. Without this, letting go of
   * the line would also drop a new waypoint wherever the drag finished.
   */
  const justDragged = useRef(false)

  // The camera the map is born with. Computed once: see the note about rebuilding above.
  const [initialOptions] = useState(() =>
    createMapOptions({
      center,
      zoom,
      ...(mapId === undefined ? {} : { mapId }),
      ...(colorScheme === undefined ? {} : { colorScheme }),
    }),
  )

  const segments = useMemo(() => toRouteSegments(legs), [legs])

  // No handler, no listeners: a route nobody is listening to should not look interactive.
  const draggable = onLegGrab !== undefined

  useEffect(() => {
    // `cancelled` is what keeps a superseded attempt from landing: a slow rejection
    // arriving after a later attempt succeeded would otherwise cover a working map with an
    // error, and the only way out would be a page reload.
    let cancelled = false
    loader().then(
      (api) => {
        if (cancelled) return
        setMaps(api)
        setError(null)
      },
      (reason: unknown) => {
        if (cancelled) return
        setError(reason instanceof Error ? reason : new Error(String(reason)))
      },
    )
    return () => {
      cancelled = true
    }
  }, [loader, attempt])

  useEffect(() => {
    const container = containerRef.current
    // The guard, not the dependency list, is what guarantees a single map: a loader that
    // resolves a second time must not replace one the user is interacting with.
    if (maps === null || container === null || mapRef.current !== null) return undefined

    const map = new maps.Map(container, initialOptions)
    mapRef.current = map

    /** Removes the handle. Safe to call more than once. */
    const clearHandle = (active: NonNullable<typeof gesture.current>): void => {
      active.handle?.detach()
    }

    const endGesture = (at: Coordinate, expectClick: boolean): void => {
      const active = gesture.current
      if (active === null) return
      clearHandle(active)
      gesture.current = null
      // Only a release *over the map* is followed by a click; one outside it is not, and
      // arming the flag then would swallow the rider's next deliberate click instead.
      justDragged.current = expectClick
      // Panning comes back on before the handler runs, so an exception in the caller
      // cannot leave the map dead to the touch.
      map.setOptions({ draggable: true })

      const travelled = distanceM(active.from, at)
      const threshold = DRAG_THRESHOLD_PX * metresPerPixel(at.lat, map.getZoom() ?? DEFAULT_ZOOM)
      if (travelled < threshold) {
        // A press that went nowhere. Routing it would spend a request and pin a waypoint
        // the rider never asked for, invisibly, on the line that is already there.
        dragHandlers.current.onLegCancel?.()
        return
      }
      dragHandlers.current.onLegDrop?.(at)
    }

    const listeners = [
      map.addListener('click', (event: google.maps.MapMouseEvent) => {
        if (gesture.current !== null) return
        // Releasing the line emits a click as well as a mouseup, and it would otherwise
        // drop a waypoint where the drag finished. Exactly one is swallowed: leaving the
        // flag set would stop the map accepting points at all, which is the worse failure
        // and a silent one.
        if (justDragged.current) {
          justDragged.current = false
          return
        }
        if (event.latLng === null) return
        onMapClickRef.current?.(toCoordinate(event.latLng))
      }),
      map.addListener('mousemove', (event: google.maps.MapMouseEvent) => {
        if (gesture.current === null || event.latLng === null) return
        const at = toCoordinate(event.latLng)
        const active = gesture.current
        active.last = at

        // Frame rate, no network, no state: this is the half of the gesture that makes it
        // feel attached to the hand between routed updates a second apart. The dot alone —
        // straight tangents from the route to the cursor read as a second, competing route
        // rather than as feedback about the one being dragged.
        active.handle?.move(toLatLng(at))

        dragHandlers.current.onLegDrag?.(at)
      }),
      map.addListener('mouseup', (event: google.maps.MapMouseEvent) => {
        if (gesture.current === null) return
        endGesture(event.latLng === null ? gesture.current.last : toCoordinate(event.latLng), true)
      }),
    ]

    // Releasing outside the map never reaches Google's mouseup, and without this the
    // gesture would never end: panning stays off and the map is dead until a reload.
    const releasedOutside = (): void => {
      const active = gesture.current
      if (active !== null) endGesture(active.last, false)
    }
    window.addEventListener('mouseup', releasedOutside)

    return () => {
      for (const listener of listeners) listener.remove()
      window.removeEventListener('mouseup', releasedOutside)
      if (gesture.current !== null) {
        clearHandle(gesture.current)
        gesture.current = null
      }
      mapRef.current = null
    }
  }, [maps, initialOptions])

  /**
   * Polylines held per leg, so only a leg that actually changed is rebuilt.
   *
   * Drag drives `legs` at the provider's throttle interval, and recreating every polyline
   * on each tick means rebuilding the whole route's geometry several times a second to move
   * one leg.
   *
   * Keyed on the *routed* geometry rather than on the `TripLeg`. Inserting a via-point
   * renumbers every following leg, so `insertVia` hands back a new TripLeg object for each
   * one — same road, shifted indices. `routed` is the thing that changes when the drawn
   * line changes, and `spliceRoutedLeg` replaces it only for the leg that was re-routed.
   */
  const legOverlays = useRef(
    new Map<
      number,
      { routed: TripLeg['routed']; lines: google.maps.Polyline[]; listeners: google.maps.MapsEventListener[] }
    >(),
  )

  useEffect(() => {
    const map = mapRef.current
    if (maps === null || map === null) return

    const overlays = legOverlays.current
    const byLeg = new Map<number, typeof segments>()
    for (const segment of segments) {
      byLeg.set(segment.legIndex, [...(byLeg.get(segment.legIndex) ?? []), segment])
    }

    legs.forEach((leg, legIndex) => {
      const cached = overlays.get(legIndex)
      if (cached !== undefined && cached.routed === leg.routed) return // same road, nothing to redraw

      for (const listener of cached?.listeners ?? []) listener.remove()
      for (const line of cached?.lines ?? []) line.setMap(null)
      const lines = (byLeg.get(legIndex) ?? []).map(
        (segment) =>
          new maps.Polyline({
            ...polylineStyle(segment.surface),
            path: segment.path.map(toLatLng),
            map,
          }),
      )
      // A right-click on the road itself, which is where "add a point here" comes from.
      //
      // Attached unconditionally, unlike the drag listener. Gating it on the prop would bake the
      // answer into this per-leg cache, which only rebuilds when a leg reroutes — a handler that
      // arrived later would go unheard until then. A no-op listener costs less than that bug.
      const contextListeners = lines.map((line) =>
        line.addListener('contextmenu', (event: google.maps.MapMouseEvent) => {
          if (event.latLng === null) return
          contextHandler.current?.({
            kind: 'route',
            legIndex,
            coordinate: toCoordinate(event.latLng),
            at: contextPointOf(event),
          })
        }),
      )
      const dragListeners = draggable
        ? lines.map((line) =>
            line.addListener('mousedown', (event: google.maps.MapMouseEvent) => {
              if (event.latLng === null) return
              const at = toCoordinate(event.latLng)
              if (dragHandlers.current.onLegGrab?.(legIndex, at) !== true) return

              const handle = createMarker(maps, {
                map,
                position: toLatLng(at),
                pin: createDragHandle(),
                advanced: hasMapId,
              })
              gesture.current = { from: at, last: at, handle }
              // Panning off for the duration, or the basemap slides under the cursor and
              // the line runs away from it.
              map.setOptions({ draggable: false })
            }),
          )
        : []
      const listeners = [...contextListeners, ...dragListeners]
      overlays.set(legIndex, { routed: leg.routed, lines, listeners })
    })

    // Legs that no longer exist take their overlays with them.
    for (const [legIndex, entry] of overlays) {
      if (legIndex < legs.length) continue
      for (const listener of entry.listeners) listener.remove()
      for (const line of entry.lines) line.setMap(null)
      overlays.delete(legIndex)
    }
    // `hasMapId` decides which marker generation the drag handle uses.
  }, [maps, legs, segments, draggable, hasMapId])

  // Unmount only: the per-leg cache above outlives individual syncs, so its teardown cannot
  // live in that effect's cleanup.
  useEffect(() => {
    const overlays = legOverlays.current
    return () => {
      for (const entry of overlays.values()) {
        for (const listener of entry.listeners) listener.remove()
        for (const line of entry.lines) line.setMap(null)
      }
      overlays.clear()
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (maps === null || map === null) return undefined

    const pins = waypoints.map((waypoint, index) => {
      const marker = createMarker(maps, {
        map,
        position: toLatLng(waypoint.coordinate),
        pin: createWaypointPin({
          kind: waypointKind(index, waypoints.length),
          label: String(index + 1),
          ...(waypoint.name === null ? {} : { name: waypoint.name }),
        }),
        advanced: hasMapId,
      })
      // Read through the ref so a new handler identity does not tear down and rebuild every
      // pin on the route — which on a long trip is a visible flicker on every render.
      const listener = marker.on('contextmenu', (event) => {
        contextHandler.current?.({ kind: 'waypoint', index, at: contextPointOf(event) })
      })
      return { marker, listener }
    })
    return () => {
      for (const { marker, listener } of pins) {
        listener?.remove()
        marker.detach()
      }
    }
  }, [maps, waypoints, hasMapId])

  useEffect(() => {
    const map = mapRef.current
    if (maps === null || map === null) return undefined

    const pins = pois.map((poi) => {
      const marker = createMarker(maps, {
        map,
        position: toLatLng(poi.coordinate),
        pin: createPoiPin(poi),
        advanced: hasMapId,
      })
      const listeners = [
        marker.on('click', () => {
          poiHandlers.current.onPoiOpen?.(poi)
        }),
        // Right-click is the add-to-route path, and it is offered only where it can work.
        ...(isVerified(poi)
          ? [
              marker.on('contextmenu', (event) => {
                contextHandler.current?.({ kind: 'poi', poi, at: contextPointOf(event) })
              }),
            ]
          : []),
      ]
      return { marker, listeners }
    })

    return () => {
      for (const { marker, listeners } of pins) {
        for (const listener of listeners) listener?.remove()
        marker.detach()
      }
    }
  }, [maps, pois, hasMapId])

  useEffect(() => {
    const map = mapRef.current
    // Frame the route once, when geometry first exists. Doing it on every change would
    // fight a user who has deliberately panned somewhere else.
    if (maps === null || map === null || hasFramedRef.current) return
    const points = segments.flatMap((segment) => segment.path)
    if (points.length === 0) return

    const bounds = new maps.LatLngBounds()
    for (const point of points) bounds.extend(toLatLng(point))
    map.fitBounds(bounds)
    hasFramedRef.current = true
  }, [maps, segments])

  return (
    <div className="map-canvas">
      <div className="map-canvas__surface" ref={containerRef} />
      {error !== null && (
        <div className="map-canvas__overlay" role="alert">
          <p className="map-canvas__message">{error.message}</p>
          <button
            type="button"
            onClick={() => {
              // Cleared here rather than in the load effect: clearing it there is a
              // synchronous setState during render commit, and cascades.
              setError(null)
              setAttempt((previous) => previous + 1)
            }}
          >
            Try again
          </button>
        </div>
      )}
      {maps === null && error === null && (
        <p className="map-canvas__overlay" role="status">
          Loading map&hellip;
        </p>
      )}
      {maps !== null && !hasMapId && (
        // A banner rather than an overlay: the map underneath works, and the rider should
        // not have to dismiss anything to use it.
        <p className="map-canvas__notice" role="status">
          {NO_MAP_ID_NOTICE}
        </p>
      )}
    </div>
  )
}
