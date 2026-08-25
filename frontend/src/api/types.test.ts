import { describe, expectTypeOf, it } from 'vitest'
import type { Coordinate, LegIntent, Poi, RouteLegResponse, Trip } from './types'

/**
 * Compile-time assertions that the generated types match what the frontend assumes.
 *
 * These fail at `tsc` time, not at runtime, so a backend contract change that breaks the
 * frontend surfaces as a red build in this file rather than as a runtime bug in a
 * component. If one of these starts failing, the contract moved — talk to the integrator
 * before "fixing" it.
 */
describe('generated API contract', () => {
  it('coordinates are lat/lon numbers', () => {
    expectTypeOf<Coordinate>().toMatchObjectType<{ lat: number; lon: number }>()
  })

  it('leg intent covers every routing mode the UI offers', () => {
    expectTypeOf<LegIntent>().toEqualTypeOf<
      'highway_connector' | 'twisty_paved' | 'unpaved' | 'technical_offroad' | 'manual_track'
    >()
  })

  it('a routed leg carries its drag-throttle budget', () => {
    expectTypeOf<RouteLegResponse['live_update_interval_ms']>().toEqualTypeOf<
      number | null | undefined
    >()
  })

  it('trips expose waypoints, legs and pois', () => {
    expectTypeOf<Trip>().toHaveProperty('waypoints')
    expectTypeOf<Trip>().toHaveProperty('legs')
    expectTypeOf<Trip>().toHaveProperty('pois')
  })

  it('persisted POIs carry no Places display data', () => {
    // Google's terms allow storing place_id and little else. If this starts compiling,
    // display data has leaked into the persisted model.
    expectTypeOf<Poi>().not.toHaveProperty('rating')
    expectTypeOf<Poi>().not.toHaveProperty('photo_urls')
    expectTypeOf<Poi>().toHaveProperty('place_id')
  })
})
