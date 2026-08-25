/// <reference types="vite/client" />

/**
 * Typed build-time environment.
 *
 * Declared rather than left to Vite's `any` index signature so a missing variable is a
 * compile error at the point of use instead of `undefined` reaching the Maps API.
 */
interface ImportMetaEnv {
  /**
   * Maps JavaScript API browser key. Necessarily public — it ships in the bundle — so it
   * must be restricted by HTTP referrer and by API, and must never be a server-side key.
   */
  readonly VITE_GOOGLE_MAPS_BROWSER_KEY?: string
  /** Cloud-configured vector map ID, required for vector rendering and advanced markers. */
  readonly VITE_GOOGLE_MAPS_MAP_ID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
