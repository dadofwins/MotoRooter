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
import { useCallback, useState } from 'react'
import { apiClient } from './api/apiClient'
import { isApiError } from './api/errors'
import type { Coordinate, Waypoint } from './api/types'
import { MapCanvas } from './map/MapCanvas'
import { MAP_ID, loadMaps } from './map/googleMaps'
import type { GoogleMapsLoader } from './map/loadGoogleMaps'
import { useRouteLeg, type LegRouter } from './trip/useRouteLeg'

export interface AppProps {
  /** Injectable so tests can drive a fake Maps API. */
  readonly mapLoader?: GoogleMapsLoader
  readonly mapId?: string
  readonly client?: LegRouter
}

/** Distance as a rider reads it, not as the API sends it. */
function formatDistance(metres: number): string {
  return `${(metres / 1000).toFixed(metres < 10_000 ? 1 : 0)} km`
}

export function App({
  mapLoader = loadMaps,
  mapId = MAP_ID,
  client = apiClient,
}: AppProps = {}): React.JSX.Element {
  const [waypoints, setWaypoints] = useState<readonly Waypoint[]>([])
  const { legs, isRouting, error } = useRouteLeg(client, waypoints)

  const addWaypoint = useCallback((coordinate: Coordinate) => {
    // Pinned: the user placed it by hand, so a later replan must not move or drop it.
    setWaypoints((previous) => [...previous, { coordinate, name: null, pinned: true }])
  }, [])

  const removeLastWaypoint = useCallback(() => {
    setWaypoints((previous) => previous.slice(0, -1))
  }, [])

  const distanceM = legs.reduce((total, leg) => total + (leg.routed?.distance_m ?? 0), 0)

  return (
    <div className="app">
      <main className="map-pane" aria-label="Route map">
        <MapCanvas
          loader={mapLoader}
          mapId={mapId}
          waypoints={waypoints}
          legs={legs}
          onMapClick={addWaypoint}
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
        {error !== null && (
          <p className="route-error" role="alert">
            {/* `code` is the stable identifier; `detail` is prose the backend may reword. */}
            {isApiError(error) && error.code === 'no_route_found'
              ? 'No route found between those points. Try moving one of them.'
              : error.message}
          </p>
        )}
      </aside>
    </div>
  )
}
