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
import { createClusterPin, createPoiPin, isVerified } from './poiPin'
import { clusterPois, type PoiCluster } from './cluster'
import { FAN_MAX_MEMBERS, fanPositions } from './fan'
import { FAN_DURATION_MS, runFanAnimation } from './fanAnimation'
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

/**
 * A right-click on an advanced marker, which the Maps API does not deliver.
 *
 * **Verified against the live API on 2026-08-26**, because every test of this feature ran against
 * a fake that happily emitted the event. `AdvancedMarkerElement` emits `click` and does not emit
 * `contextmenu`: same marker, same content, same dispatch, one line apart in the probe, with and
 * without `gmpClickable`. So the whole right-click menu — remove a point, add a place to the
 * route, hide it — was correct, tested, green, and did nothing at all wherever a Map ID is
 * configured, which is production.
 *
 * The content is a real DOM node on this generation, and the probe confirmed the rest of the
 * chain: the pin is hit-testable at its own screen position, and a right-click there reaches a
 * listener on that node with the coordinates intact. So the event is taken from the DOM and
 * wrapped in the shape the Maps API would have used, which keeps the shape of every caller.
 *
 * The plain-marker fallback keeps `addListener`: `google.maps.Marker` documents `contextmenu`,
 * and the probe could not synthesise a click that reached one to check — recorded as unverified
 * rather than assumed either way.
 */
function onPinContextMenu(
  pin: HTMLElement,
  handler: (event: unknown) => void,
): google.maps.MapsEventListener {
  const forward = (event: MouseEvent): void => {
    handler({ domEvent: event })
  }
  pin.addEventListener('contextmenu', forward)
  return {
    remove: () => {
      pin.removeEventListener('contextmenu', forward)
    },
  }
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
      on: (event, handler) =>
        event === 'contextmenu'
          ? onPinContextMenu(input.pin, handler)
          : marker.addListener(event, handler),
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

/** A pin the POI layer drew, with anything drawn alongside it. */
interface DrawnPin {
  readonly marker: AttachedMarker
  readonly listeners: readonly (google.maps.MapsEventListener | null)[]
  /** The line back to the group and its casing, on a fanned member. */
  readonly leader?: readonly google.maps.Polyline[]
  /** The group this pin is a member of, on a fanned member: what the opening animates around. */
  readonly opening?: PoiCluster
}

/**
 * The line from an opened group to one of its members, and the casing under it.
 *
 * Quiet and thin: it is a piece of explanation, not a route, and it must not read as one on a map
 * whose whole subject is a line. But quiet is not the same as invisible, and a single mid-grey
 * stroke measured 3.88:1 against the *flat pane tone* — which is not the surface it sits on.
 * Rendered over satellite imagery it vanishes into the pale bands and merges into the dark ones.
 *
 * A light casing under a dark line is the same answer the pins already use, one dimension down:
 * they survive any basemap because of their white ring, not because of their fill.
 */
const FAN_LEADER_CASING: google.maps.PolylineOptions = {
  strokeColor: '#ffffff',
  strokeOpacity: 0.9,
  strokeWeight: 4,
  clickable: false,
  zIndex: 1,
}

const FAN_LEADER_STYLE: google.maps.PolylineOptions = {
  strokeColor: '#374151',
  strokeOpacity: 0.95,
  strokeWeight: 1.5,
  clickable: false,
  zIndex: 2,
}

export interface PoiClusterOpened {
  readonly members: readonly Poi[]
  readonly at: ScreenPoint
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
  /**
   * Somewhere to look while there is nothing better — the rider's own position, in practice.
   *
   * Applied only while no route has ever been framed. A trip on screen is where they want to be
   * looking, and a position arriving late must not pull the camera off it; a route arriving
   * *after* this still frames, because being centred somewhere is not the same as having framed
   * a route.
   */
  readonly focus?: Coordinate
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
  /**
   * A group of overlapping places was clicked.
   *
   * The pin says how many; only the caller can say which, so this hands over the members and a
   * point to open a disclosure at. Never fires for a group of one — that is drawn as the place
   * itself and reported through `onPoiOpen`.
   */
  readonly onClusterOpen?: (cluster: PoiClusterOpened) => void
}

/**
 * How far the pointer must travel before a press counts as a drag.
 *
 * In screen pixels rather than metres: at zoom 18 a few pixels is a couple of metres and at
 * zoom 8 it is a kilometre, so a distance threshold would swallow real drags when zoomed in
 * and invent them when zoomed out.
 */
const DRAG_THRESHOLD_PX = 5

/**
 * How long after a release a click may still be the release's own.
 *
 * Generous against a slow frame and far short of a rider's next deliberate click, which is
 * hundreds of milliseconds away at best. The API turns out not to send one at all after a drag,
 * so in practice this expires unused — it exists so that the behaviour is correct if that ever
 * changes back, rather than because it is load-bearing today.
 */
const POST_DRAG_CLICK_MS = 250

/**
 * How close to zoom when showing the rider where they are.
 *
 * "Roughly" was the word in the request, and roughly is right: this is a hint about where to
 * start, not a claim about precision. Around a hundred kilometres across — a day's ride, so the
 * roads they might actually plan on are on screen — against the opening camera's whole-BDR-country
 * view, which is useful only for knowing the app works.
 */
const FOCUS_ZOOM = 9

/** A waypoint's identity for the camera's purposes: where it is, to about a metre. */
function waypointKey(at: Coordinate): string {
  return `${at.lat.toFixed(5)},${at.lon.toFixed(5)}`
}

/**
 * Whether what is on screen is a *different route* rather than an edit to the framed one.
 *
 * Framing once and never again was too coarse, and Tim found the case: an assistant plotting a
 * whole trip is not a change to the route on screen, it is a different route arriving, and the
 * rider has expressed no camera preference about a trip that did not exist a second ago.
 *
 * The distinction is **edited versus replaced**, and waypoint identity draws it where a count or
 * a first-time flag cannot. A drag inserts a via between two points that are still there; an
 * append keeps everything; a mode change keeps every waypoint and replaces every coordinate of
 * the geometry, which is why this reads waypoints and not the line.
 *
 * A route with no waypoints at all — legs handed in on their own — cannot be told apart this way,
 * so it keeps the old rule and is framed exactly once. Silence is not evidence of replacement.
 */
function isReplacement(framed: readonly string[] | null, current: readonly string[]): boolean {
  if (framed === null) return true
  if (framed.length === 0 || current.length === 0) return false
  return current.every((key) => !framed.includes(key))
}

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
  onClusterOpen,
  focus,
}: MapCanvasProps): React.JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<google.maps.Map | null>(null)
  /**
   * The waypoints the camera was last framed on, or null before it ever has been.
   *
   * A set rather than a flag, because the question is not *whether* the route has been framed
   * but whether what is on screen is still the route that was framed — see `isReplacement`.
   */
  const framedOn = useRef<readonly string[] | null>(null)
  const onMapClickRef = useRef(onMapClick)

  const [maps, setMaps] = useState<GoogleMaps | null>(null)
  /**
   * The zoom the pins are grouped at.
   *
   * State rather than a read at draw time, because whether two pins overlap depends on it: the
   * grouping has to be redone when the rider zooms, and zooming in is how they take a group
   * apart. Seeded from the initial camera so the first draw is not grouped at the wrong scale.
   */
  const [currentZoom, setCurrentZoom] = useState(zoom ?? DEFAULT_ZOOM)
  /**
   * The group currently opened into its members, by cluster key.
   *
   * Keyed on the members rather than on a position, so a group that survives a redraw stays open
   * and one that no longer exists closes itself.
   */
  const [fannedKey, setFannedKey] = useState<string | null>(null)
  /** Read inside the map's own click handler, which is registered once and outlives this state. */
  const fanned = useRef<string | null>(null)
  useEffect(() => {
    fanned.current = fannedKey
  }, [fannedKey])
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
    onClusterOpen?: ((cluster: PoiClusterOpened) => void) | undefined
  }>({})
  useEffect(() => {
    poiHandlers.current = { onPoiOpen, onClusterOpen }
  }, [onPoiOpen, onClusterOpen])

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
   * When a drag ended, so the click that may follow it can be told from the rider's next one.
   *
   * **Measured against the live API, and it goes the other way.** A trusted press-release that
   * stays put does emit a `click`; a trusted press-move-release does not — Maps suppresses it
   * because the pointer travelled. Both halves were checked, since "no click after a drag" says
   * nothing without a control confirming a click is producible at all.
   *
   * A boolean armed at drag-end therefore waited for something that never came, and swallowed
   * the rider's *next deliberate* click instead: one lost waypoint per drag, silently, which is
   * the failure the old comment here called the worse one.
   *
   * A timestamp rather than a flag, because it is right whichever way the API behaves. A real
   * post-release click arrives in the same tick; an absent one simply expires.
   */
  const draggedAt = useRef<number | null>(null)

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
      draggedAt.current = expectClick ? Date.now() : null
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
        // A click that dismisses something does not also do a thing. Same reasoning as the
        // swallowed post-drag click below: closing an open fan by dropping a waypoint under it
        // is not what the rider asked for.
        if (fanned.current !== null) {
          setFannedKey(null)
          return
        }
        // Releasing the line emits a click as well as a mouseup, and it would otherwise
        // drop a waypoint where the drag finished. Exactly one is swallowed: leaving the
        // flag set would stop the map accepting points at all, which is the worse failure
        // and a silent one.
        if (draggedAt.current !== null && Date.now() - draggedAt.current <= POST_DRAG_CLICK_MS) {
          draggedAt.current = null
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
      map.addListener('zoom_changed', () => {
        setCurrentZoom(map.getZoom() ?? DEFAULT_ZOOM)
        // The grouping is about to be redone, and the group this fan belongs to may not survive
        // it. Leader lines pointing at a hub that no longer exists are worse than a closed fan.
        setFannedKey(null)
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

  /**
   * Places grouped by whether their pins would overlap at this zoom.
   *
   * Redone when the rider zooms, which is what makes zooming in a way to take a group apart.
   */
  const clusters = useMemo(() => clusterPois(pois, { zoom: currentZoom }), [pois, currentZoom])

  useEffect(() => {
    const map = mapRef.current
    if (maps === null || map === null) return undefined

    /** One place, drawn wherever it has been put. */
    const placePin = (place: Poi, at: Coordinate): DrawnPin => {
      const marker = createMarker(maps, {
        map,
        position: toLatLng(at),
        pin: createPoiPin(place),
        advanced: hasMapId,
      })
      return {
        marker,
        listeners: [
          marker.on('click', () => {
            poiHandlers.current.onPoiOpen?.(place)
          }),
          // Right-click is the add-to-route path, and it is offered only where it can work.
          // Fanned members get it too: clustering had otherwise taken the idiom away from every
          // place in a group, because the group's pin was the only thing left to click.
          ...(isVerified(place)
            ? [
                marker.on('contextmenu', (event) => {
                  contextHandler.current?.({ kind: 'poi', poi: place, at: contextPointOf(event) })
                }),
              ]
            : []),
        ],
      }
    }

    const pins = clusters.flatMap((cluster): DrawnPin[] => {
      const [only] = cluster.members
      // A group of one is the place itself. Drawing a "1" over it would say a rider has
      // something to open when they have exactly what they can already see.
      if (cluster.members.length === 1 && only !== undefined) {
        return [placePin(only, cluster.coordinate)]
      }

      const open = cluster.key === fannedKey

      const hub = createMarker(maps, {
        map,
        position: toLatLng(cluster.coordinate),
        // The members, not just how many: the pin wears their colours in proportion.
        pin: createClusterPin(cluster.members),
        advanced: hasMapId,
      })
      const hubPin: DrawnPin = {
        marker: hub,
        listeners: [
          hub.on('click', (event) => {
            // The thing that opened it closes it.
            if (open) {
              setFannedKey(null)
              return
            }
            // Small enough to open on the map; anything bigger goes to the caller as a list,
            // because a fan that size overlaps itself and solves nothing.
            if (cluster.members.length <= FAN_MAX_MEMBERS) {
              setFannedKey(cluster.key)
              return
            }
            poiHandlers.current.onClusterOpen?.({
              members: cluster.members,
              at: contextPointOf(event),
            })
          }),
        ],
      }

      if (!open) return [hubPin]

      // Opened. Each member is drawn away from the group, with a line back to it: a fanned pin
      // is not where its place is, and the line is what makes that a disclosed offset rather
      // than a quiet relocation. The group's own pin stays as the hub — without it the lines
      // converge on nothing, and there is no way to close what was opened.
      //
      // Built closed, at the group itself, and opened by the animation below. Nothing waits for
      // it: the pins exist and are clickable from the first frame.
      const closed = fanPositions(cluster.coordinate, cluster.members.length, currentZoom, 0)
      return [
        hubPin,
        ...cluster.members.map((member, index) => {
          const at = closed[index] ?? cluster.coordinate
          const path = [toLatLng(cluster.coordinate), toLatLng(at)]
          const leader = [FAN_LEADER_CASING, FAN_LEADER_STYLE].map(
            (style) => new maps.Polyline({ ...style, path, map }),
          )
          return { ...placePin(member, at), leader, opening: cluster }
        }),
      ]
    })

    /**
     * Open the fan.
     *
     * One loop over the pins *and* their lines, because they are different kinds of overlay and
     * only this layer can see both — a CSS transition on a marker's content would move the pins
     * and leave the strokes at full extent, which reads worse than no motion at all. It is also
     * why no React animation library can do this: nothing React renders is moving.
     */
    const opening = pins.filter((pin) => pin.opening !== undefined)
    const cluster = opening[0]?.opening
    const stopAnimating =
      cluster === undefined
        ? undefined
        : runFanAnimation(FAN_DURATION_MS, (progress) => {
            const spots = fanPositions(
              cluster.coordinate,
              cluster.members.length,
              currentZoom,
              progress,
            )
            const hub = toLatLng(cluster.coordinate)
            opening.forEach(({ marker, leader }, index) => {
              const at = toLatLng(spots[index] ?? cluster.coordinate)
              marker.move(at)
              for (const line of leader ?? []) line.setPath([hub, at])
            })
          })

    return () => {
      stopAnimating?.()
      for (const { marker, listeners, leader } of pins) {
        for (const listener of listeners) listener?.remove()
        marker.detach()
        for (const line of leader ?? []) line.setMap(null)
      }
    }
  }, [maps, clusters, hasMapId, fannedKey, currentZoom])

  useEffect(() => {
    const map = mapRef.current
    if (maps === null || map === null || focus === undefined) return
    // Only while the camera has no better claim. `framedOn` is the record of a route having
    // taken it, so this reads that rather than keeping a second flag that could disagree.
    if (framedOn.current !== null) return
    map.setCenter(toLatLng(focus))
    map.setZoom(FOCUS_ZOOM)
  }, [maps, focus])

  useEffect(() => {
    const map = mapRef.current
    if (maps === null || map === null) return
    const points = segments.flatMap((segment) => segment.path)
    if (points.length === 0) return

    const identity = waypoints.map((point) => waypointKey(point.coordinate))
    if (!isReplacement(framedOn.current, identity)) return

    const bounds = new maps.LatLngBounds()
    for (const point of points) bounds.extend(toLatLng(point))
    map.fitBounds(bounds)
    framedOn.current = identity
  }, [maps, segments, waypoints])

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
