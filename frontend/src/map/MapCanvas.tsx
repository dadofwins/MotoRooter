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
import type { Coordinate, TripLeg, Waypoint } from '../api/types'
import type { GoogleMaps, GoogleMapsLoader } from './loadGoogleMaps'
import { createMapOptions, toCoordinate, toLatLng, type MapColorScheme } from './mapOptions'
import { polylineStyle, toRouteSegments } from './routeLayer'
import { createWaypointPin, waypointKind } from './waypointPin'

/** Stable identities: inline defaults would rebuild every overlay on every render. */
const NO_LEGS: readonly TripLeg[] = []
const NO_WAYPOINTS: readonly Waypoint[] = []

/** Opening view: BDR country, wide enough to place a first point anywhere in it. */
const DEFAULT_CENTER: Coordinate = { lat: 44.5, lon: -116.5 }
const DEFAULT_ZOOM = 5

export interface MapCanvasProps {
  /**
   * Resolves the Maps API. Must be a stable reference — create it once at module scope,
   * not inline in a render.
   */
  readonly loader: GoogleMapsLoader
  readonly waypoints?: readonly Waypoint[]
  readonly legs?: readonly TripLeg[]
  /** Initial camera only; later changes do not move a map the user may be panning. */
  readonly center?: Coordinate
  readonly zoom?: number
  readonly mapId?: string
  readonly colorScheme?: MapColorScheme
  /** A click on the basemap, in domain coordinates. The mouse path for setting points. */
  readonly onMapClick?: (coordinate: Coordinate) => void
}

export function MapCanvas({
  loader,
  waypoints = NO_WAYPOINTS,
  legs = NO_LEGS,
  center = DEFAULT_CENTER,
  zoom = DEFAULT_ZOOM,
  mapId,
  colorScheme,
  onMapClick,
}: MapCanvasProps): React.JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<google.maps.Map | null>(null)
  const hasFramedRef = useRef(false)
  const onMapClickRef = useRef(onMapClick)

  const [maps, setMaps] = useState<GoogleMaps | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [attempt, setAttempt] = useState(0)

  // Read at click time rather than captured, so a new handler does not mean a new listener.
  useEffect(() => {
    onMapClickRef.current = onMapClick
  }, [onMapClick])

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
    const listener = map.addListener('click', (event: google.maps.MapMouseEvent) => {
      if (event.latLng === null) return
      onMapClickRef.current?.(toCoordinate(event.latLng))
    })

    return () => {
      listener.remove()
      mapRef.current = null
    }
  }, [maps, initialOptions])

  useEffect(() => {
    const map = mapRef.current
    if (maps === null || map === null) return undefined

    const polylines = segments.map(
      (segment) =>
        new maps.Polyline({
          ...polylineStyle(segment.surface),
          path: segment.path.map(toLatLng),
          map,
        }),
    )
    return () => {
      for (const polyline of polylines) polyline.setMap(null)
    }
  }, [maps, segments])

  useEffect(() => {
    const map = mapRef.current
    if (maps === null || map === null) return undefined

    const markers = waypoints.map((waypoint, index) => {
      const name = waypoint.name
      return new maps.marker.AdvancedMarkerElement({
        map,
        position: toLatLng(waypoint.coordinate),
        content: createWaypointPin({
          kind: waypointKind(index, waypoints.length),
          label: String(index + 1),
          ...(name === null || name === undefined ? {} : { name }),
        }),
        ...(name === null || name === undefined ? {} : { title: name }),
      })
    })
    return () => {
      for (const marker of markers) marker.map = null
    }
  }, [maps, waypoints])

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
    </div>
  )
}
