/**
 * Shell layout: big map on the left, chat rail on the right.
 *
 * The split exists to keep one rule honest — every action must be reachable with the mouse as
 * well as by typing. So the shell owns the route and grows it from map clicks: a rider can
 * place a start and an end, drag the line, add a place, and see it saved, without touching the
 * assistant at all.
 *
 * Trip state is held as **local edits tagged with the stored trip they were made against**.
 * When a newer document arrives — from a shared link, or re-read because somebody else's edit
 * won the compare-and-swap — those edits no longer describe it, and what is stored simply
 * shows. That is the "stored wins" rule expressed as a comparison rather than as a state
 * update inside an effect, which cascades renders and then needs a guard to stop looping.
 *
 * Which duration may be shown, and which may not:
 *
 * `RouteLeg.duration_s` may not. On dirt it comes from a bicycle profile and reads about twice
 * as long as a motorcycle takes, and planning is duration-driven, so a four-hour day shown as
 * eight makes day-splitting nonsense. `RouteLegResponse.estimated_duration_s` may — derived
 * server-side from distance and surface, so the speed table has one home rather than a copy
 * per client. `ascent_m` remains unexplained against its reference and stays off screen.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiClient } from './api/apiClient'
import type { ApiClient } from './api/client'
import type { Coordinate, Poi, Trip, TripLeg, Waypoint } from './api/types'
import { MapCanvas } from './map/MapCanvas'
import { MAP_ID, loadMaps } from './map/googleMaps'
import type { GoogleMapsLoader } from './map/loadGoogleMaps'
import { isVerified } from './map/poiPin'
import { PoiDetailDialog } from './poi/PoiDetailDialog'
import { DragSession } from './routing/dragSession'
import { addPoiToRoute, type RouteEdit } from './routing/tripEdits'
import { routeErrorMessage } from './trip/routeErrorMessage'
import { SurfaceSummary } from './trip/SurfaceSummary'
import { useRouteLeg } from './trip/useRouteLeg'
import { useRoutingCapabilities } from './trip/useRoutingCapabilities'
import { useStoredTrip, useTripSave } from './trip/useTripDocument'
import { formatDistance, formatDuration } from './units/format'
import { useDistanceUnit } from './units/useDistanceUnit'

/** Only the calls the shell makes, so a test double stays small. */
type AppClient = Pick<
  ApiClient,
  'routeLeg' | 'routingCapabilities' | 'placeDetail' | 'createTrip' | 'getTrip' | 'updateTrip'
>

/** The intent a dragged leg keeps. Matches the one the slice routes with. */
const DRAG_INTENT = 'unpaved'

const NO_POIS: readonly Poi[] = []

/**
 * What the rider has changed, and the stored trip they changed it from.
 *
 * `base` is compared by identity: a different document means these edits describe something
 * that no longer exists.
 */
interface Edited {
  readonly base: Trip | null
  readonly waypoints: readonly Waypoint[]
  readonly pois: readonly Poi[]
  /**
   * Geometry a drag or a load already holds, which the routing hook must not re-request.
   * Freshness is decided from each leg's fingerprint, so offering a stale one costs nothing.
   */
  readonly legs: readonly TripLeg[] | null
}

export interface AppProps {
  /** Injectable so tests can drive a fake Maps API. */
  readonly mapLoader?: GoogleMapsLoader
  readonly mapId?: string
  readonly client?: AppClient
  /** Places to start with. A loaded trip's own POIs replace these. */
  readonly pois?: readonly Poi[]
}

export function App({
  mapLoader = loadMaps,
  mapId = MAP_ID,
  client = apiClient,
  pois = NO_POIS,
}: AppProps = {}): React.JSX.Element {
  const { unit, setUnit } = useDistanceUnit()

  /**
   * The stored document, read directly rather than copied into state.
   *
   * Copying it meant a setState inside an effect watching for it, which cascades renders. The
   * comparison below does the same job without one.
   */
  const { trip: stored, reload } = useStoredTrip(client)
  const [edit, setEdit] = useState<Edited>({ base: null, waypoints: [], pois, legs: null })

  /** The stored document, as an edit nobody has changed yet. */
  const fromStored = useCallback(
    (): Edited => ({
      base: stored,
      waypoints: stored?.waypoints ?? [],
      pois: stored?.pois ?? pois,
      legs: stored?.legs ?? null,
    }),
    [stored, pois],
  )

  // The trip as it currently is: the rider's edits while they still apply, and the stored
  // document once they do not.
  const live: Edited = edit.base === stored ? edit : fromStored()
  const waypoints = live.waypoints
  const placed = live.pois

  /** Applies a change to whichever version is live, so an edit never resurrects a stale one. */
  const change = useCallback(
    (next: (from: Edited) => Partial<Edited>) => {
      setEdit((previous) => {
        const from = previous.base === stored ? previous : fromStored()
        return { ...from, ...next(from) }
      })
    },
    [stored, fromStored],
  )

  const capabilities = useRoutingCapabilities(client)
  /**
   * The value, not the object it came from.
   *
   * A DragSession keyed on the capabilities object is rebuilt whenever that object's identity
   * changes. A preview landing mid-drag re-renders, so the gesture was destroyed by its own
   * progress: the release had nothing to end and the rider's drag disappeared.
   */
  const dragIntervalMs = capabilities.intervalFor(DRAG_INTENT)

  const { legs, estimatedDurationS, isRouting, error } = useRouteLeg(client, waypoints, live.legs)

  /** Provisional geometry during a gesture. Never saved, never in undo history. */
  const [preview, setPreview] = useState<readonly TripLeg[] | null>(null)
  const shownLegs = preview ?? legs

  // The state a gesture starts from, read when the line is grabbed rather than captured in a
  // handler. Synced in an effect, not during render: a ref written while rendering is unsafe
  // under concurrent rendering.
  const current = useRef<RouteEdit>({ waypoints, legs })
  useEffect(() => {
    current.current = { waypoints, legs }
  }, [waypoints, legs])

  const addWaypoint = useCallback(
    (coordinate: Coordinate) => {
      change((from) => ({
        // Pinned: the rider placed it by hand, so a later replan must not move or drop it.
        waypoints: [...from.waypoints, { coordinate, name: null, pinned: true }],
        // Whatever geometry was in hand no longer describes this route.
        legs: null,
      }))
    },
    [change],
  )

  const removeLastWaypoint = useCallback(() => {
    change((from) => ({ waypoints: from.waypoints.slice(0, -1), legs: null }))
  }, [change])

  const drag = useMemo(
    () =>
      new DragSession({
        client,
        // From the API, never a constant: unknown resolves to preview-only.
        intervalMs: dragIntervalMs,
        onPreview: (previewed) => {
          setPreview(previewed.legs)
        },
        onCommit: (committed) => {
          setPreview(null)
          // Handed back so the routing hook recognises it as already answered, rather than
          // spending a second request for the route the drag just fetched.
          change(() => ({ waypoints: committed.waypoints, legs: committed.legs }))
        },
        onError: () => {
          // The route reverts to what was last committed rather than keeping a preview the
          // server never agreed to.
          setPreview(null)
        },
      }),
    // Rebuilt when the cadence arrives, so the first drag after load is not stuck on
    // preview-only for the rest of the session — and at no other time.
    [client, dragIntervalMs, change],
  )

  const onLegGrab = useCallback(
    (legIndex: number, at: Coordinate) => drag.begin(current.current, { legIndex, grabbed: at }),
    [drag],
  )

  // The line that follows the cursor is drawn by the canvas, imperatively, at pointer speed.
  // Nothing local happens here on purpose: routing a cursor position through the state that
  // also feeds `legs` is what made the session rebuild itself mid-gesture.
  const onLegDrag = useCallback(
    (at: Coordinate) => {
      drag.update(at)
    },
    [drag],
  )
  const onLegDrop = useCallback(
    (at: Coordinate) => {
      drag.release(at)
    },
    [drag],
  )
  const onLegCancel = useCallback(() => {
    // A press that went nowhere. Nothing was routed and nothing should be shown.
    drag.cancel()
    setPreview(null)
  }, [drag])

  // A gesture outliving its component would deliver a commit into a dead tree.
  useEffect(
    () => () => {
      drag.cancel()
    },
    [drag],
  )

  /** What the rider last asked about. */
  const [openPoi, setOpenPoi] = useState<Poi | null>(null)

  const onPoiAdd = useCallback(
    (poi: Poi) => {
      change((from) => {
        const added = addPoiToRoute({ waypoints: from.waypoints, legs: current.current.legs }, poi)
        return added === null ? {} : { waypoints: added.waypoints, legs: null }
      })
    },
    [change],
  )

  const onPoiOpen = useCallback((poi: Poi) => {
    setOpenPoi(poi)
  }, [])

  /**
   * Saving: created on the first waypoint, written on a debounce, addressed by a slug in the
   * URL. Without it nothing survived a reload and nothing could be shared, and both chat and
   * replan are addressed by that slug.
   */
  const save = useTripSave(
    client,
    useMemo(() => ({ waypoints, legs, pois: placed }), [waypoints, legs, placed]),
    // On a conflict the stored document has won, so it is re-read and the comparison above
    // drops the edits that no longer describe it.
    useMemo(() => ({ slug: stored?.slug ?? null, onConflict: reload }), [stored?.slug, reload]),
  )

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
          pois={placed}
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
              {distanceM > 0 && ` · ${formatDistance(distanceM, unit)}`}
              {estimatedDurationS !== null && ` · ${formatDuration(estimatedDurationS)}`}
              {isRouting && ' · routing…'}
            </p>
            <button type="button" onClick={removeLastWaypoint}>
              Remove last point
            </button>
          </div>
        )}

        <SurfaceSummary legs={shownLegs} unit={unit} />

        {save.slug !== null && (
          // The link is the sharing model, so it is said rather than left in the address bar:
          // this trip is world-readable by design and a rider should know that.
          <p className="trip-saved" aria-live="polite">
            {save.status === 'saving' || save.status === 'creating'
              ? 'Saving…'
              : 'Saved — this link is shareable.'}
          </p>
        )}

        {save.status === 'conflict' && (
          <p className="route-error" role="alert">
            Somebody else edited this trip first, so your change was replaced by theirs.
          </p>
        )}
        {save.status === 'failed' && save.error !== null && (
          <p className="route-error" role="alert">
            {routeErrorMessage(save.error)}
          </p>
        )}

        <div className="units">
          {/* A preference, so it sits with the numbers it changes rather than in a settings
              screen nobody opens. */}
          <button type="button" aria-pressed={unit === 'mi'} onClick={() => setUnit('mi')}>
            Miles
          </button>
          <button type="button" aria-pressed={unit === 'km'} onClick={() => setUnit('km')}>
            Kilometres
          </button>
        </div>

        {openPoi !== null && (
          <PoiDetailDialog
            poi={openPoi}
            client={client}
            onClose={() => setOpenPoi(null)}
            // Absent for an unconfirmed suggestion, so the dialog shows no control that could
            // not work.
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
            {/* Never `error.message`: that is an internal string, and a network outage and a
                server bug would read identically. */}
            {routeErrorMessage(error)}
          </p>
        )}
      </aside>
    </div>
  )
}
