import { describe, expect, it } from 'vitest'
import { tripBlurbKey } from './tripBlurbKey'
import {
  poi as poiFixture,
  routeLeg,
  trip as tripFixture,
  tripLeg,
} from '../api/fixtures'
import type { Poi, Trip, TripLeg, Waypoint } from '../api/types'

/**
 * What counts as a different trip, for the purpose of spending a model call on describing it.
 *
 * The quota guarantee is structural and lives at the call site: the key is only ever computed
 * from the *committed* trip document, so mid-drag preview geometry has no path to it and
 * cannot spend a request whatever this function is made of. This file is the second line —
 * given two committed documents, is the trip meaningfully different?
 *
 * The field this deliberately ignores is the coordinate. `Waypoint` has no id, so "the same
 * waypoint" has to be recognised by something, and the coordinate is precisely the field that
 * moves while nothing about the trip changes: nudging a point along the road it is already on
 * is not a new trip. Names, modes, place categories and a coarse distance are what a line
 * about the trip would actually draw on.
 */

function waypoint(name: string | null, lat = 47, lon = -120): Waypoint {
  return { coordinate: { lat, lon }, name, pinned: false }
}

function leg(intent: TripLeg['intent'], distanceM: number, index: number): TripLeg {
  return tripLeg({
    intent,
    start_waypoint_index: index,
    end_waypoint_index: index + 1,
    routed: routeLeg({ distance_m: distanceM, intent }),
  })
}

function poi(category: Poi['category'], id: string): Poi {
  return poiFixture({ id, category })
}

function tripWith(overrides: Partial<Trip>): Trip {
  return { ...tripFixture(), ...overrides }
}

describe('tripBlurbKey', () => {
  it('is stable for a document that has not changed', () => {
    const trip = tripWith({ waypoints: [waypoint('Ellensburg'), waypoint('Cashmere')] })

    expect(tripBlurbKey(trip)).toBe(tripBlurbKey(tripWith({ ...trip })))
  })

  it('ignores a waypoint nudged along the road it is already on', () => {
    // The case this exists for. A drag commit that keeps the same named places is the same
    // trip, and paying an LLM call for it is the quota failure arriving through a new door.
    const before = tripWith({ waypoints: [waypoint('Ellensburg', 47.0032, -120.5478)] })
    const after = tripWith({ waypoints: [waypoint('Ellensburg', 47.0041, -120.5461)] })

    expect(tripBlurbKey(after)).toBe(tripBlurbKey(before))
  })

  it('changes when a waypoint is added', () => {
    const before = tripWith({ waypoints: [waypoint('Ellensburg')] })
    const after = tripWith({ waypoints: [waypoint('Ellensburg'), waypoint('Cashmere')] })

    expect(tripBlurbKey(after)).not.toBe(tripBlurbKey(before))
  })

  it('changes when a waypoint is renamed, which is how a move to somewhere else shows up', () => {
    const before = tripWith({ waypoints: [waypoint('Ellensburg')] })
    const after = tripWith({ waypoints: [waypoint('Leavenworth')] })

    expect(tripBlurbKey(after)).not.toBe(tripBlurbKey(before))
  })

  it('changes when the same places are visited in a different order', () => {
    // A reversed route is a different ride, and a set would call it the same one.
    const out = tripWith({ waypoints: [waypoint('Ellensburg'), waypoint('Cashmere')] })
    const back = tripWith({ waypoints: [waypoint('Cashmere'), waypoint('Ellensburg')] })

    expect(tripBlurbKey(back)).not.toBe(tripBlurbKey(out))
  })

  it('changes when a leg switches riding mode', () => {
    // Mode is per-leg, so this reads the legs rather than `default_intent` — a trip whose
    // default never moves can still turn from dirt into tarmac one segment at a time.
    const dirt = tripWith({ legs: [leg('unpaved', 40_000, 0)] })
    const fast = tripWith({ legs: [leg('highway_connector', 40_000, 0)] })

    expect(tripBlurbKey(fast)).not.toBe(tripBlurbKey(dirt))
  })

  it('changes when places are swapped for a different kind at the same count', () => {
    // Count alone is blind to this, and it is exactly what the line is about: a rider who
    // switches wild camping for hotels and still reads a blurb about dirt camping has caught
    // the feature lying.
    const camping = tripWith({ pois: [poi('campground', 'a'), poi('campground', 'b')] })
    const hotels = tripWith({ pois: [poi('hotel', 'a'), poi('hotel', 'b')] })

    expect(tripBlurbKey(hotels)).not.toBe(tripBlurbKey(camping))
  })

  it('changes when one place of a kind becomes several', () => {
    // The count does reach the prose — a live line read "26% unpaved and a few camps" off one
    // campground and two wild camps — so "a camp" standing while there are five would be the
    // same lie as a stale blurb.
    const one = tripWith({ pois: [poi('campground', 'a')] })
    const several = tripWith({
      pois: [poi('campground', 'a'), poi('campground', 'b'), poi('campground', 'c')],
    })

    expect(tripBlurbKey(several)).not.toBe(tripBlurbKey(one))
  })

  it('does not change between two counts a rider would describe the same way', () => {
    // Five camps and six camps is not a different ride, and this is what stops the header
    // churning while discovery streams places in one at a time.
    const five = tripWith({ pois: ['a', 'b', 'c', 'd', 'e'].map((id) => poi('campground', id)) })
    const six = tripWith({
      pois: ['a', 'b', 'c', 'd', 'e', 'f'].map((id) => poi('campground', id)),
    })

    expect(tripBlurbKey(six)).toBe(tripBlurbKey(five))
  })

  it('does not churn while a replan streams places in one at a time', () => {
    // Backend's measurement, kept as a test rather than a note. `ReplanEvent.pois` is
    // documented as cumulative per stage and `useReplan` is built for incremental arrival, so
    // the day discovery streams them, one entry per POI would be one model call per event —
    // a header rewriting itself through a two-minute replan.
    const categories = ['campground', 'hotel', 'fuel', 'food', 'viewpoint', 'wild_camp'] as const
    const arriving = Array.from({ length: 80 }, (_, index) =>
      poi(categories[index % categories.length] as Poi['category'], `poi-${String(index)}`),
    )

    const keysFor = (count: number): Set<string> =>
      new Set(
        arriving
          .slice(0, count)
          .map((_, index) => tripBlurbKey(tripWith({ pois: arriving.slice(0, index + 1) }))),
      )

    // One entry per POI gives one key per arrival. Measured at 40 before this changed.
    expect(keysFor(40).size).toBeLessThan(20)

    // The property, rather than a figure to tune: doubling how many places arrive does not
    // produce more regenerations. The ceiling is the number of *kinds* on the route times the
    // buckets, so it is bounded by the vocabulary rather than by discovery's output.
    expect(keysFor(80).size).toBe(keysFor(40).size)
  })

  it('does not change when the same places are listed in a different order', () => {
    // Discovery does not promise an order, and a re-run that returns the same places
    // shuffled is not a different trip.
    const first = tripWith({ pois: [poi('campground', 'a'), poi('hotel', 'b')] })
    const shuffled = tripWith({ pois: [poi('hotel', 'b'), poi('campground', 'a')] })

    expect(tripBlurbKey(shuffled)).toBe(tripBlurbKey(first))
  })

  it('ignores a distance change too small to be a different ride', () => {
    const before = tripWith({ waypoints: [waypoint(null)], legs: [leg('unpaved', 40_000, 0)] })
    const after = tripWith({ waypoints: [waypoint(null)], legs: [leg('unpaved', 41_000, 0)] })

    expect(tripBlurbKey(after)).toBe(tripBlurbKey(before))
  })

  it('changes when an unnamed waypoint moves far enough to change the distance bucket', () => {
    // The safety net for the gap this design accepts: an unnamed waypoint has no name to
    // change, so a long drag would otherwise read as the same trip. Distance catches it once
    // the move is big, which is the case where it matters.
    const before = tripWith({ waypoints: [waypoint(null)], legs: [leg('unpaved', 40_000, 0)] })
    const after = tripWith({ waypoints: [waypoint(null)], legs: [leg('unpaved', 95_000, 0)] })

    expect(tripBlurbKey(after)).not.toBe(tripBlurbKey(before))
  })

  it('reads distance from the legs, not from the stored total', () => {
    // `total_distance_m` is on the allowlist as deliberately unread, because the rail
    // recomputes from legs so its figure is live during an edit. Reading it here would make
    // that entry false. The legs are the source either way.
    const trip = tripWith({ legs: [leg('unpaved', 95_000, 0)], total_distance_m: 1 })
    const sameLegs = tripWith({ legs: [leg('unpaved', 95_000, 0)], total_distance_m: 999_999 })

    expect(tripBlurbKey(sameLegs)).toBe(tripBlurbKey(trip))
  })

  it('survives a leg that has never been routed', () => {
    // An unroutable segment leaves `routed` null, and a trip mid-edit routinely has one.
    const trip = tripWith({ legs: [{ ...leg('unpaved', 0, 0), routed: null }] })

    expect(() => tripBlurbKey(trip)).not.toThrow()
  })
})
