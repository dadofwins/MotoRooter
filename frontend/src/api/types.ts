/**
 * Ergonomic aliases over the generated OpenAPI types.
 *
 * `schema.ts` is generated from `shared/openapi.json` and must never be hand-edited —
 * `npm run generate:types` overwrites it, and CI fails if it drifts from the backend.
 * This file is the hand-written surface: import from here, not from `schema.ts`.
 *
 * If a type you need is missing, add an alias here rather than reaching into
 * `components['schemas']` at the call site.
 */
import type { components } from './schema'

type Schemas = components['schemas']

// Geography and routing
export type Coordinate = Schemas['Coordinate']
export type RouteLeg = Schemas['RouteLeg']
export type SurfaceSpan = Schemas['SurfaceSpan']
export type Surface = Schemas['Surface']
export type LegIntent = Schemas['LegIntent']
export type ProviderCapabilities = Schemas['ProviderCapabilities']

// Trips
export type Trip = Schemas['Trip']
export type TripSummary = Schemas['TripSummary']
export type TripLeg = Schemas['TripLeg']
export type Waypoint = Schemas['Waypoint']

// Points of interest
export type Poi = Schemas['Poi']
export type PoiDetail = Schemas['PoiDetail']
export type PoiCategory = Schemas['PoiCategory']
export type PoiSource = Schemas['PoiSource']

// Requests and responses
export type RouteLegRequest = Schemas['RouteLegRequest']
export type RouteLegResponse = Schemas['RouteLegResponse']
export type CreateTripRequest = Schemas['CreateTripRequest']
export type UpdateTripRequest = Schemas['UpdateTripRequest']
export type ReplanRequest = Schemas['ReplanRequest']
export type ReplanEvent = Schemas['ReplanEvent']
export type RoutingCapabilitiesResponse = Schemas['RoutingCapabilitiesResponse']
export type IntentRouting = Schemas['IntentRouting']
export type ErrorResponse = Schemas['ErrorResponse']

/**
 * Stable error codes from `ErrorResponse.code`. Switch on these rather than on the
 * human-readable `detail`, which is not part of the contract.
 */
export type ApiErrorCode =
  | 'invalid_slug'
  | 'trip_not_found'
  | 'trip_already_exists'
  | 'trip_storage_unavailable'
  | 'validation_error'
  | 'invalid_request'
  | 'unsupported_intent'
  | 'provider_not_found'
  | 'no_route_found'
  | 'quota_exceeded'
  | 'provider_unavailable'
