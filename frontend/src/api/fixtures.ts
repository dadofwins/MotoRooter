/**
 * Builders for API response shapes, for use in tests.
 *
 * Exists because hand-written object literals typed as a response shape are a maintenance
 * tax that falls on the wrong person. Every field the backend adds is required — a response
 * always carries it, and making it optional would tell the client a number might be absent
 * when it never is — so each addition breaks every literal at once, in files the backend
 * engineer is not allowed to edit. That happened three times in a day: `estimated_duration_s`,
 * `reports_surface`, and the derived trip metrics, each costing a blocked handoff and a
 * round trip through the integrator.
 *
 * A builder moves that cost to one place. Adding a field means one edit here, and every test
 * keeps compiling with a sensible default. Tests that care about a value pass it; tests that
 * do not stay silent about it, which also makes them read better — a fixture listing eleven
 * fields when the test is about one is noise.
 *
 * These are deliberately typed against the GENERATED types rather than hand-written shapes.
 * A builder that drifted from the contract would be worse than the literals it replaced,
 * because it would hide the drift in one place instead of surfacing it in four.
 */
import type {
  Coordinate,
  Poi,
  RouteLeg,
  RouteLegResponse,
  Trip,
  TripLeg,
  TripSummary,
  Waypoint,
} from './types'

export function coordinate(lat = 47.0, lon = -121.0): Coordinate {
  return { lat, lon }
}

export function waypoint(lat = 47.0, lon = -121.0, overrides: Partial<Waypoint> = {}): Waypoint {
  return { coordinate: coordinate(lat, lon), name: null, pinned: true, ...overrides }
}

export function routeLeg(overrides: Partial<RouteLeg> = {}): RouteLeg {
  return {
    geometry: [coordinate(47.0), coordinate(47.5)],
    distance_m: 1000,
    duration_s: 60,
    surface_spans: [],
    ascent_m: null,
    provider: 'fake',
    intent: 'unpaved',
    routed_from: null,
    ...overrides,
  }
}

export function tripLeg(overrides: Partial<TripLeg> = {}): TripLeg {
  return {
    intent: 'unpaved',
    start_waypoint_index: 0,
    end_waypoint_index: 1,
    provider_override: null,
    routed: routeLeg(),
    last_routing_error: null,
    ...overrides,
  }
}

export function routeLegResponse(overrides: Partial<RouteLegResponse> = {}): RouteLegResponse {
  return {
    leg: routeLeg(),
    live_update_interval_ms: 1000,
    estimated_duration_s: 60,
    ...overrides,
  }
}

/** The figures the backend derives and always sends. Never absent from a response. */
const DERIVED_METRICS = {
  total_distance_m: 0,
  total_paved_fraction: 0,
  total_unpaved_fraction: 0,
  total_unknown_fraction: 0,
  estimated_duration_s: 0,
} as const

export function trip(overrides: Partial<Trip> = {}): Trip {
  return {
    schema_version: 1,
    slug: 'wabdr-north',
    name: 'WABDR North',
    created_at: '2026-08-25T18:00:00Z',
    edited_at: '2026-08-25T18:00:00Z',
    planned_at: null,
    waypoints: [],
    legs: [],
    pois: [],
    ...DERIVED_METRICS,
    ...overrides,
  }
}

export function tripSummary(overrides: Partial<TripSummary> = {}): TripSummary {
  return {
    slug: 'wabdr-north',
    name: 'WABDR North',
    created_at: '2026-08-25T18:00:00Z',
    edited_at: '2026-08-25T18:00:00Z',
    needs_replan: false,
    ...DERIVED_METRICS,
    ...overrides,
  }
}

export function poi(overrides: Partial<Poi> = {}): Poi {
  return {
    id: 'poi-1',
    name: 'Halfway Flat Dispersed Campground',
    category: 'wild_camp',
    coordinate: coordinate(47.2, -121.2),
    source: 'places',
    place_id: 'ChIJ_example',
    on_route: false,
    note: null,
    ...overrides,
  }
}
