/**
 * The app's single Maps API loader.
 *
 * Created once at module scope, deliberately: `MapCanvas` treats the loader as an identity,
 * and a new one on every render would re-run the load effect. This is also the only place
 * the environment is read, so nothing else has to know how the key is configured.
 *
 * Local setup — `frontend/.env.local`, which is gitignored:
 *
 *     VITE_GOOGLE_MAPS_BROWSER_KEY=...   # restricted to Maps JavaScript API + this origin
 *     VITE_GOOGLE_MAPS_MAP_ID=...        # a vector map ID from the Cloud console
 */
import { createGoogleMapsLoader } from './loadGoogleMaps'

export const loadMaps = createGoogleMapsLoader({
  apiKey: import.meta.env.VITE_GOOGLE_MAPS_BROWSER_KEY ?? '',
  // `marker` supplies AdvancedMarkerElement, which replaced the deprecated Marker.
  libraries: ['maps', 'marker'],
})

/** Empty when unset, which `createMapOptions` reads as "no vector map ID configured". */
export const MAP_ID = import.meta.env.VITE_GOOGLE_MAPS_MAP_ID ?? ''
