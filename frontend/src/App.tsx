/**
 * Shell layout: big map on the left, chat rail on the right.
 *
 * The split exists to keep one rule honest — every action must be reachable with the mouse
 * as well as by typing. So the shell owns the route's waypoints and grows them from map
 * clicks: a user can place a start and an end without touching the chat rail at all.
 *
 * Routing those waypoints into legs is the next piece of work; the canvas already draws
 * legs when it is given them.
 */
import { useCallback, useState } from 'react'
import type { Coordinate, Waypoint } from './api/types'
import { MapCanvas } from './map/MapCanvas'
import { MAP_ID, loadMaps } from './map/googleMaps'
import type { GoogleMapsLoader } from './map/loadGoogleMaps'

export interface AppProps {
  /** Injectable so tests can drive a fake Maps API. */
  readonly mapLoader?: GoogleMapsLoader
  readonly mapId?: string
}

export function App({ mapLoader = loadMaps, mapId = MAP_ID }: AppProps = {}): React.JSX.Element {
  const [waypoints, setWaypoints] = useState<readonly Waypoint[]>([])

  const addWaypoint = useCallback((coordinate: Coordinate) => {
    // Pinned: the user placed it by hand, so a later replan must not move or drop it.
    setWaypoints((previous) => [...previous, { coordinate, name: null, pinned: true }])
  }, [])

  const removeLastWaypoint = useCallback(() => {
    setWaypoints((previous) => previous.slice(0, -1))
  }, [])

  return (
    <div className="app">
      <main className="map-pane" aria-label="Route map">
        <MapCanvas
          loader={mapLoader}
          mapId={mapId}
          waypoints={waypoints}
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
            </p>
            <button type="button" onClick={removeLastWaypoint}>
              Remove last point
            </button>
          </div>
        )}
      </aside>
    </div>
  )
}
