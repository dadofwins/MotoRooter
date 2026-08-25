/**
 * Shell layout: big map on the left, chat rail on the right.
 *
 * The split exists to keep one rule honest — every action must be reachable with the mouse
 * as well as by typing. So the shell owns the route's waypoints and grows them from map
 * clicks: a rider can place a start and an end, and see a real route drawn between them,
 * without touching the chat rail at all.
 *
 * This is the vertical slice: click, `POST /api/routing/leg`, draw. Persistence, drag and
 * the assistant are all still to come, and none of them changes this path.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiClient } from './api/apiClient'
import type { ApiClient } from './api/client'
import type { Coordinate, Poi, TripLeg, Waypoint } from './api/types'
import { MapCanvas } from './map/MapCanvas'
import { isVerified } from './map/poiPin'
import { PoiDetailDialog } from './poi/PoiDetailDialog'
import { MAP_ID, loadMaps } from './map/googleMaps'
import type { GoogleMapsLoader } from './map/loadGoogleMaps'
import { DragSession } from './routing/dragSession'
import { addPoiToRoute, type RouteEdit } from './routing/tripEdits'
import { routeErrorMessage } from './trip/routeErrorMessage'
import { useRouteLeg } from './trip/useRouteLeg'
import { useRoutingCapabilities } from './trip/useRoutingCapabilities'

/** Only the two calls the shell makes, so a test double stays small. */
type AppClient = Pick<ApiClient, 'routeLeg' | 'routingCapabilities' | 'placeDetail'>

/** The intent a dragged leg keeps. Matches the one the slice routes with. */
const DRAG_INTENT = 'unpaved'

export interface AppProps {
  /** Injectable so tests can drive a fake Maps API. */
  readonly mapLoader?: GoogleMapsLoader
  readonly mapId?: string
  readonly client?: AppClient
  /**
   * Places to show on the map.
   *
   * Nothing produces these yet — discovery reaches the frontend through replan, which still
   * answers 501 — so today they arrive only from a caller or a loaded trip.
   */
  readonly pois?: readonly Poi[]
}

/**
 * Distance as a rider reads it, not as the API sends it.
 *
 * Distance is the only routed figure shown, deliberately. `RouteLeg.duration_s` comes from
 * a bicycle profile on the dirt provider and reads about 2x long — eight hours for a
 * four-hour day — and trip planning is duration-driven, so a wrong number is worse than
 * none. `ascent_m` is similarly unexplained against its reference. Neither goes on screen
 * until the backend derives a figure it trusts.
 */
function formatDistance(metres: number): string {
  return `${(metres / 1000).toFixed(metres < 10_000 ? 1 : 0)} km`
}

const NO_POIS: readonly Poi[] = []

export function App({
  mapLoader = loadMaps,
  mapId = MAP_ID,
  client = apiClient,
  pois = NO_POIS,
}: AppProps = {}): React.JSX.Element {
  const [waypoints, setWaypoints] = useState<readonly Waypoint[]>([])
  /**
   * Geometry a drag produced, which the hook must not re-request.
   *
   * A drag routes its leg itself on release, so handing the result back means the hook can
   * recognise it as already-answered — by fingerprint, not by call order — instead of
   * spending a second request per drag.
   */
  const [draggedLegs, setDraggedLegs] = useState<readonly TripLeg[] | null>(null)

  const capabilities = useRoutingCapabilities(client)
  /**
   * The value, not the object it came from.
   *
   * A DragSession keyed on the capabilities object is rebuilt whenever that object's
   * identity changes — which, before it was memoised, was every render. A preview landing
   * mid-drag re-renders, so the gesture was destroyed by its own progress: the release then
   * had nothing to end, and the rider's drag disappeared. Keying on the number means only a
   * genuine change of cadence rebuilds it.
   */
  const dragIntervalMs = capabilities.intervalFor(DRAG_INTENT)
  const { legs, isRouting, error } = useRouteLeg(client, waypoints, draggedLegs)

  /** Provisional geometry during a gesture. Never saved, never in undo history. */
  const [preview, setPreview] = useState<readonly TripLeg[] | null>(null)

  // The state a gesture starts from, read when the line is grabbed rather than captured in
  // a handler. Synced in an effect, not during render: a ref written while rendering is
  // unsafe under concurrent rendering.
  const current = useRef<RouteEdit>({ waypoints, legs })
  useEffect(() => {
    current.current = { waypoints, legs }
  }, [waypoints, legs])

  const addWaypoint = useCallback((coordinate: Coordinate) => {
    // Pinned: the user placed it by hand, so a later replan must not move or drop it.
    setWaypoints((previous) => [...previous, { coordinate, name: null, pinned: true }])
    // Whatever a drag produced no longer describes this route; let the hook route it.
    setDraggedLegs(null)
  }, [])

  const removeLastWaypoint = useCallback(() => {
    setWaypoints((previous) => previous.slice(0, -1))
    setDraggedLegs(null)
  }, [])

  const drag = useMemo(
    () =>
      new DragSession({
        client,
        // From the API, never a constant: unknown resolves to preview-only.
        intervalMs: dragIntervalMs,
        onPreview: (edit) => {
          setPreview(edit.legs)
        },
        onCommit: (edit) => {
          setPreview(null)
          setDraggedLegs(edit.legs)
          setWaypoints(edit.waypoints)
        },
        onError: () => {
          // The route reverts to what was last committed rather than keeping a preview the
          // server never agreed to.
          setPreview(null)
        },
      }),
    // Rebuilt when the cadence arrives, so the first drag after load is not stuck on
    // preview-only for the rest of the session — and at no other time.
    [client, dragIntervalMs],
  )

  const onLegGrab = useCallback(
    (legIndex: number, at: Coordinate) => drag.begin(current.current, { legIndex, grabbed: at }),
    [drag],
  )

  // The line that follows the cursor is drawn by the canvas, imperatively, at pointer
  // speed. Nothing local happens here on purpose: routing a cursor position through the
  // state that also feeds `legs` is what made the session rebuild itself mid-gesture.
  const onLegDrag = useCallback((at: Coordinate) => { drag.update(at) }, [drag])
  const onLegDrop = useCallback((at: Coordinate) => { drag.release(at) }, [drag])

  /** What the rider last asked about, shown in the rail. */
  const [openPoi, setOpenPoi] = useState<Poi | null>(null)

  const onPoiAdd = useCallback((poi: Poi) => {
    setWaypoints((previous) => {
      const added = addPoiToRoute({ waypoints: previous, legs: current.current.legs }, poi)
      return added?.waypoints ?? previous
    })
    // The route no longer matches what a drag produced, so let the hook route it.
    setDraggedLegs(null)
  }, [])

  const onPoiOpen = useCallback((poi: Poi) => {
    setOpenPoi(poi)
  }, [])

  const onLegCancel = useCallback(() => {
    // A press that went nowhere. Nothing was routed and nothing should be shown.
    drag.cancel()
    setPreview(null)
  }, [drag])

  // A gesture outliving its component would deliver a commit into a dead tree.
  useEffect(() => () => { drag.cancel() }, [drag])

  const shownLegs = preview ?? legs
  const distanceM = shownLegs.reduce((total, leg) => total + (leg.routed?.distance_m ?? 0), 0)

  return (
    <div className="app">
      <main className="map-pane" aria-label="Route map">
        <MapCanvas
          loader={mapLoader}
          mapId={mapId}
          waypoints={waypoints}
          legs={shownLegs}
          onMapClick={addWaypoint}
          onLegGrab={onLegGrab}
          onLegDrag={onLegDrag}
          onLegDrop={onLegDrop}
          onLegCancel={onLegCancel}
          pois={pois}
          onPoiAdd={onPoiAdd}
          onPoiOpen={onPoiOpen}
        />
      </main>
      <aside className="chat-pane" aria-label="Trip assistant">
        <p className="greeting">
          Describe your trip and I&rsquo;ll help plan it for you! Or set a start and end point on
          the map.
        </p>
        {waypoints.length > 0 && (
          <div className="route-summary">
            {/* Stated in words as well as drawn, so the map is not the only feedback. */}
            <p aria-live="polite">
              {waypoints.length} point{waypoints.length === 1 ? '' : 's'} placed
              {distanceM > 0 && ` · ${formatDistance(distanceM)}`}
              {isRouting && ' · routing…'}
            </p>
            <button type="button" onClick={removeLastWaypoint}>
              Remove last point
            </button>
          </div>
        )}
        {openPoi !== null && (
          <PoiDetailDialog
            poi={openPoi}
            client={client}
            onClose={() => setOpenPoi(null)}
            // Absent for an unconfirmed suggestion, so the dialog shows no control that
            // could not work.
            {...(isVerified(openPoi)
              ? {
                  onAddToRoute: (poi: Poi) => {
                    onPoiAdd(poi)
                    setOpenPoi(null)
                  },
                }
              : {})}
          />
        )}
        {error !== null && (
          <p className="route-error" role="alert">
            {/* Never `error.message`: that is an internal string, and a network outage and
                a server bug would read identically. */}
            {routeErrorMessage(error)}
          </p>
        )}
      </aside>
    </div>
  )
}
