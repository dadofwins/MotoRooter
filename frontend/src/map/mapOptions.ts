/**
 * Map options, in one place, as deliberate design decisions.
 *
 * Pure and free of the `google.maps` runtime so the choices are reviewable and testable
 * without an API key. The reasoning for each lives in `mapOptions.test.ts`.
 */
import type { Coordinate } from '../api/types'

/** `FOLLOW_SYSTEM` unless the app pins one; dark mode is not optional outdoors. */
export type MapColorScheme = 'FOLLOW_SYSTEM' | 'LIGHT' | 'DARK'

export interface MapOptionsInput {
  readonly center: Coordinate
  readonly zoom: number
  /** Cloud-configured vector map ID. Required for vector rendering and advanced markers. */
  readonly mapId?: string
  readonly colorScheme?: MapColorScheme
}

/** Google speaks `lng`; the domain model speaks `lon`. Convert only at this boundary. */
export function toLatLng(coordinate: Coordinate): google.maps.LatLngLiteral {
  return { lat: coordinate.lat, lng: coordinate.lon }
}

export function toCoordinate(latLng: google.maps.LatLng): Coordinate {
  return { lat: latLng.lat(), lon: latLng.lng() }
}

export function createMapOptions(input: MapOptionsInput): google.maps.MapOptions {
  const mapId = input.mapId ?? ''

  return {
    center: toLatLng(input.center),
    zoom: input.zoom,
    renderingType: 'VECTOR',
    ...(mapId === '' ? {} : { mapId }),
    colorScheme: input.colorScheme ?? 'FOLLOW_SYSTEM',

    // One-finger pan. The alternative makes a phone in a parking lot infuriating.
    gestureHandling: 'greedy',
    // Every click here means something to this app. Google's own place icons would
    // otherwise intercept them and open an info window of their own.
    clickableIcons: false,

    // Satellite earns its space: it is how a rider judges whether a track is a real road.
    mapTypeControl: true,
    streetViewControl: false,
    fullscreenControl: false,
    keyboardShortcuts: true,
  }
}
