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

// Service
export type HealthResponse = Schemas['HealthResponse']

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
export type PoiDetailResponse = Schemas['PoiDetailResponse']

// Assistant conversation
export type ChatRequest = Schemas['ChatRequest']
export type ChatEvent = Schemas['ChatEvent']
export type ChatTurn = Schemas['ChatTurn']
export type RoutingCapabilitiesResponse = Schemas['RoutingCapabilitiesResponse']
export type IntentRouting = Schemas['IntentRouting']
export type ErrorResponse = Schemas['ErrorResponse']

/**
 * Makes the request fields `K` optional, for fields the backend gives a default.
 *
 * `openapi-typescript` marks any schema field with a default as *required*. That is right
 * for a response — the server always fills it in — and wrong for a request body, where
 * omitting the field is precisely how you ask for the default. Without this, every call
 * site would have to restate `avoid_tolls: false`, freezing the backend's current defaults
 * into the frontend.
 *
 * Only optionality is adjusted; the field types still come from the generated schema. If
 * the backend drops one of these defaults, the `Pick` stops compiling rather than drifting
 * quietly — which is the whole point of not hand-writing the shape.
 */
type DefaultsOptional<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>

/** `RouteLegRequest` with the backend-defaulted routing flags made optional. */
export type RouteLegInput = DefaultsOptional<
  RouteLegRequest,
  'avoid_tolls' | 'avoid_highways' | 'avoid_ferries' | 'want_elevation'
>

/**
 * Stable error codes from `ErrorResponse.code`. Switch on these rather than on the
 * human-readable `detail`, which is not part of the contract.
 *
 * Generated, not hand-maintained. It used to be a literal union restated here, which was a
 * silent drift channel: the backend derived codes from Python exception class names, so a
 * rename changed the wire contract with no build failure on either side. Codes are now
 * declared explicitly on the backend and published as an OpenAPI enum, so a change to the
 * set breaks the build here instead.
 */
export type ApiErrorCode = Schemas['ErrorCode']
