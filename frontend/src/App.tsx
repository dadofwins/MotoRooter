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
import { addPoiToRoute, isLegStale, type RouteEdit } from './routing/tripEdits'
import { replanErrorMessage, routeErrorMessage } from './trip/routeErrorMessage'
import { SurfaceSummary } from './trip/SurfaceSummary'
import { ChatRail } from './chat/ChatRail'
import { Landing } from './landing/Landing'
import {
  DEFAULT_INTENT,
  legsSpanning,
  withWaypointAppended,
  withWaypointRemoved,
} from './routing/legStructure'
import { ReplanProgress } from './trip/ReplanProgress'
import { RoutePoints } from './trip/RoutePoints'
import { needsReplan, useReplan } from './trip/useReplan'
import { useRouteLegs } from './trip/useRouteLegs'
import { useRoutingCapabilities } from './trip/useRoutingCapabilities'
import { clearTripFromUrl, hasTripInUrl, useStoredTrip, useTripSave } from './trip/useTripDocument'
import { useVisitedTrips } from './trip/useVisitedTrips'
import { formatDistance, formatDuration } from './units/format'
import { useDistanceUnit } from './units/useDistanceUnit'

/** Only the calls the shell makes, so a test double stays small. */
type AppClient = Pick<
  ApiClient,
  | 'routeLeg'
  | 'routingCapabilities'
  | 'placeDetail'
  | 'createTrip'
  | 'getTrip'
  | 'updateTrip'
  | 'replan'
  | 'chat'
>

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

/**
 * Leaving a trip remounts rather than resets.
 *
 * The alternative was a `reset` on each of the three hooks that hold trip state — the stored
 * document, the save, the replan — and the next piece of state added would have needed a
 * fourth. Worse, missing one is silent and expensive: with the previous document still loaded,
 * the first waypoint of the *next* trip was written to the *previous* trip's slug, replacing a
 * trip the rider had just been looking at. A remount cannot forget a field.
 *
 * `autoOpenAllowed` is what stops the remount from undoing itself: the rider with one trip
 * would otherwise be auto-opened straight back into the trip they just left.
 */
export function App(props: AppProps = {}): React.JSX.Element {
  const [session, setSession] = useState(0)
  return (
    <TripSession
      key={session}
      {...props}
      autoOpenAllowed={session === 0}
      onLeave={() => setSession((previous) => previous + 1)}
    />
  )
}

interface TripSessionProps extends AppProps {
  /** False after the rider has explicitly asked for a new trip. */
  readonly autoOpenAllowed: boolean
  readonly onLeave: () => void
}

function TripSession({
  mapLoader = loadMaps,
  mapId = MAP_ID,
  client = apiClient,
  pois = NO_POIS,
  autoOpenAllowed,
  onLeave,
}: TripSessionProps): React.JSX.Element {
  const { unit, setUnit } = useDistanceUnit()
  const visited = useVisitedTrips()

  /**
   * A trip to open without asking: the only one this browser knows, and only when the URL
   * names none.
   *
   * Decided once, at mount. A link must still go to the trip it names, whatever this browser
   * has seen — that is what the URL check is for, and dropping it makes two fetches race.
   */
  const [autoOpen] = useState<string | null>(() => {
    if (!autoOpenAllowed || hasTripInUrl()) return null
    const only = visited.trips.length === 1 ? visited.trips[0] : undefined
    return only?.slug ?? null
  })

  /**
   * Whether the rider has come through the front door yet.
   *
   * The landing screen is the entrance and the map is what is behind it. A URL naming a trip
   * skips straight through, which is what makes a shared link work — and so does a browser
   * that knows exactly one trip.
   */
  // Derived from the same decision rather than restating its condition: two expressions of one
  // fact is how they come to disagree.
  const [entered, setEntered] = useState(() => hasTripInUrl() || autoOpen !== null)
  /** The name typed at the front door, carried into the trip this session creates. */
  const [chosenName, setChosenName] = useState<string | null>(null)

  /**
   * The stored document, read directly rather than copied into state.
   *
   * Copying it meant a setState inside an effect watching for it, which cascades renders. The
   * comparison below does the same job without one.
   */
  const { trip: stored, reload, open } = useStoredTrip(client)

  // Reading this browser's list is an external system, so opening from it belongs in an
  // effect. Runs once: the decision was made at mount and must not be revisited.
  useEffect(() => {
    if (autoOpen !== null) open(autoOpen)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, at mount, by design
  }, [])
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
   * The lookup, not a resolved number.
   *
   * Cadence is per leg now, because a trip's legs are not served by one engine, so the session
   * resolves it at every grab from the leg the rider actually took hold of.
   *
   * Still not the capabilities object itself. A DragSession keyed on that is rebuilt whenever
   * its identity changes, and a preview landing mid-drag re-renders — so the gesture was
   * destroyed by its own progress, the release had nothing to end, and the rider's drag
   * disappeared. `intervalFor` is memoised with the response, so it changes once, on load.
   */
  const intervalFor = capabilities.intervalFor

  /**
   * The trip's leg structure: one leg per pair of waypoints, each with its own intent.
   *
   * Derived when the document has none — a trip saved before legs were real, or one whose
   * waypoints arrived without them. Healing it here rather than at load keeps the rule that the
   * stored document is read, never rewritten behind the rider's back.
   */
  const structure = useMemo(
    () => live.legs ?? legsSpanning(waypoints.length, DEFAULT_INTENT),
    [live.legs, waypoints.length],
  )

  const { legs, estimatedDurationS, isRouting, error, unroutableCount } = useRouteLegs(
    client,
    waypoints,
    structure,
  )

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
      change((from) =>
        // One new leg to reach it; every existing leg keeps the geometry it already has. This
        // used to discard all of it, so each click re-routed the whole trip.
        withWaypointAppended(
          { waypoints: from.waypoints, legs: from.legs ?? legsSpanning(from.waypoints.length, DEFAULT_INTENT) },
          // Pinned: the rider placed it by hand, so a later replan must not move or drop it.
          { coordinate, name: null, pinned: true },
          DEFAULT_INTENT,
        ),
      )
    },
    [change],
  )

  /**
   * Remove any point, not just the last one.
   *
   * `withWaypointRemoved` has always accepted an arbitrary index; until now nothing in the UI
   * could reach one. That mattered as soon as the assistant was given `remove_waypoint`: chat
   * would have been the only way to take a via-point out of the middle of a route, which is the
   * one thing the accelerator rule forbids.
   */
  const removeWaypoint = useCallback(
    (index: number) => {
      change((from) => {
        if (index < 0 || index >= from.waypoints.length) return {}
        return withWaypointRemoved(
          {
            waypoints: from.waypoints,
            legs: from.legs ?? legsSpanning(from.waypoints.length, DEFAULT_INTENT),
          },
          index,
        )
      })
    },
    [change],
  )

  const drag = useMemo(
    () =>
      new DragSession({
        client,
        // From the API, never a constant: the grabbed leg's own intent decides, and an
        // intent the table does not mention resolves to preview-only.
        intervalFor,
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
    // Rebuilt when the capability table arrives, so the first drag after load is not stuck on
    // preview-only for the rest of the session — and at no other time.
    [client, intervalFor, change],
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
        // The re-shaped legs, not `null`. Discarding them re-derived a pairwise structure and
        // split the leg the place was inserted into, so adding one campground cost two
        // requests and re-routed the segment either side of it.
        return added === null ? {} : added
      })
    },
    [change],
  )

  const onPoiOpen = useCallback((poi: Poi) => {
    setOpenPoi(poi)
  }, [])

  /**
   * The slow path. Explicitly triggered, streamed into the map, and never blocking the fast
   * one — dragging during a replan keeps working because nothing here waits on it.
   */
  const replan = useReplan(client)

  /**
   * Whether the suggestions are stale relative to the route.
   *
   * Derived rather than read: the flag is serialised on `TripSummary` and not on `Trip`. Shown
   * on the button because stale suggestions a rider cannot detect are worse than none.
   */
  const stale = needsReplan(stored)

  /**
   * Places on the map: the trip's own, plus whatever the running replan has found so far.
   *
   * A union rather than a merge into state. Copying the stream into the trip needed a setState
   * inside an effect watching it, which cascades renders; deriving gives pins that appear as
   * they resolve and still get saved, because this is what the save is fed.
   */
  const placed = useMemo(() => {
    if (replan.pois.length === 0) return live.pois
    const known = new Set(live.pois.map((poi) => poi.id))
    return [...live.pois, ...replan.pois.filter((found) => !known.has(found.id))]
  }, [live.pois, replan.pois])

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
    useMemo(
      () => ({
        slug: stored?.slug ?? null,
        onConflict: reload,
        ...(chosenName === null ? {} : { name: chosenName }),
      }),
      [stored?.slug, reload, chosenName],
    ),
  )

  const distanceM = shownLegs.reduce((total, leg) => total + (leg.routed?.distance_m ?? 0), 0)

  /**
   * The riding time to show, and where it may come from.
   *
   * Per-leg estimates when this session routed every leg. Otherwise the figure the backend
   * computed for the stored document — but only while every leg still matches what was stored,
   * because the moment the rider changes one, that number describes a trip they no longer have.
   * Nothing in between: a partial total read as the whole day is worse than no figure, and this
   * is the number a rider plans a day around.
   */
  const untouched = structure.length > 0 && structure.every((leg) => !isLegStale(waypoints, leg))
  const shownDurationS =
    estimatedDurationS ?? (untouched ? (stored?.estimated_duration_s ?? null) : null)

  // This browser's record of where it has been, updated whenever a trip is known — created
  // here, or arrived at by link.
  const knownSlug = save.slug
  // A trip created this session is never re-read, so its name comes from the save rather than
  // from a stored document that will stay null all session.
  const knownName = stored?.name ?? save.name
  const remember = visited.remember
  useEffect(() => {
    if (knownSlug !== null) remember({ slug: knownSlug, name: knownName ?? 'Untitled trip' })
  }, [knownSlug, knownName, remember])

  if (!entered) {
    return (
      <Landing
        trips={visited.trips}
        onCreate={(name) => {
          // Carried into the first save; an empty one takes the default.
          setChosenName(name === '' ? null : name)
          setEntered(true)
        }}
        onOpen={(slug) => {
          open(slug)
          setEntered(true)
        }}
        onForget={visited.forget}
      />
    )
  }

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
          onWaypointRemove={removeWaypoint}
        />
      </main>
      <aside className="chat-pane" aria-label="Trip assistant">
        <div className="trip-bar">
          {knownName !== null && (
            // Which trip this is, whether it was arrived at by link or created here. Keyed on
            // the name rather than on `stored`, which stays null for a trip created this
            // session — so this heading never rendered on the path that creates trips.
            <h1 className="trip-name">{knownName}</h1>
          )}
          {/* Always reachable, which is what auto-opening the only trip costs: the rider who
              has exactly one would otherwise have no way to start another. */}
          <button
            type="button"
            className="trip-bar__new"
            onClick={() => {
              // The URL first: it names the current trip, and the remount reads it back.
              clearTripFromUrl()
              onLeave()
            }}
          >
            New trip
          </button>
        </div>
        {waypoints.length > 0 && (
          <div className="route-summary">
            {/* Stated in words as well as drawn, so the map is not the only feedback. */}
            <p aria-live="polite">
              {waypoints.length} point{waypoints.length === 1 ? '' : 's'} placed
              {distanceM > 0 && ` · ${formatDistance(distanceM, unit)}`}
              {shownDurationS !== null && ` · ${formatDuration(shownDurationS)}`}
              {isRouting && ' · routing…'}
            </p>
          </div>
        )}

        {/* The mouse's reach over the route, point by point. Right-click on a pin is the fast
            path; this is the discoverable and keyboard-reachable one. */}
        <RoutePoints waypoints={waypoints} onRemove={removeWaypoint} />

        <SurfaceSummary legs={shownLegs} unit={unit} />

        {save.slug !== null && (
          <div className="replan">
            <button
              type="button"
              onClick={() => {
                replan.start(save.slug ?? '')
              }}
              disabled={replan.isRunning || waypoints.length < 2}
            >
              {replan.isRunning ? 'Finding places…' : 'Find places along the route'}
            </button>
            {/* An accumulating log rather than one replaced line: this run takes minutes, and
                a rider watching it needs to see it progress. Kept after it ends as a record. */}
            <ReplanProgress
              isRunning={replan.isRunning}
              log={replan.log}
              progress={replan.progress}
              elapsedS={replan.elapsedS}
            />
            {!replan.isRunning && stale && placed.length > 0 && (
              // The route moved after these were found, so they describe a different trip.
              <p className="replan__stale">
                The route has changed since these places were found.
              </p>
            )}
            {!replan.isRunning && replan.foundNothing && (
              // A real outcome, and a common one today. Better said than left as an empty map.
              <p className="replan__progress">No places found along this route.</p>
            )}
            {replan.error !== null && (
              <p className="route-error" role="alert">
                {/* Its own mapping: a 501 here means this instance has no credentials, not
                    that the feature is unfinished. */}
                {replanErrorMessage(replan.error)}
              </p>
            )}
          </div>
        )}

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

        {/* The accelerator, not the requirement. It sits below the route it talks about, and
            everything it can do is reachable with the mouse above it. `reload` rather than a
            local merge: the assistant edited the document, so the document wins — replaying
            events into state here would make two models of one trip. */}
        <ChatRail client={client} resolveSlug={save.ensure} onTripChanged={reload} />

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

        {unroutableCount > 0 && (
          // Said plainly rather than left as a hole in the line. One dead segment does not
          // stop the rest of the trip being useful, but a rider who cannot see which part
          // failed has no way to fix it.
          <p className="route-warning" role="status">
            {unroutableCount} segment{unroutableCount === 1 ? '' : 's'} could not be routed. The
            rest of the route is unchanged — try moving the points on either side.
          </p>
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
