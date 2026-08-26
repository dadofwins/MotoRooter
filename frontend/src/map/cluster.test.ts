import { describe, expect, it } from 'vitest'
import { CLUSTER_RADIUS_PX, clusterPois, pixelsAt } from './cluster'
import { poi as poiFixture } from '../api/fixtures'
import type { Coordinate, Poi } from '../api/types'

/**
 * Grouping pins that land on top of each other.
 *
 * Discovery returns places along a corridor, and a corridor is narrow: several campgrounds off
 * the same forest road are hundreds of metres apart, which at the zoom that shows a whole day's
 * ride is a few pixels. The pins then cover each other, and the one on top is whichever happened
 * to be drawn last — so a rider cannot see how many places are there, cannot click the ones
 * underneath, and has no way to know they exist.
 *
 * Clustering is decided in **screen space, not in metres**. The question is whether two pins
 * overlap, and that depends on the zoom: the same two places are one blob at zoom 8 and two
 * clearly separate pins at zoom 14.
 */

function place(id: string, lat: number, lon: number): Poi {
  return poiFixture({ id, name: id, coordinate: { lat, lon } })
}

/** Two points a given number of pixels apart at a given zoom, near the corridor's latitude. */
function apartBy(pixels: number, zoom: number): [Coordinate, Coordinate] {
  const from: Coordinate = { lat: 47.5, lon: -120.5 }
  const origin = pixelsAt(from, zoom)
  // Solved by walking east, which is linear in longitude at any latitude.
  const perDegree = pixelsAt({ lat: 47.5, lon: -119.5 }, zoom).x - origin.x
  return [from, { lat: 47.5, lon: -120.5 + pixels / perDegree }]
}

describe('pixelsAt', () => {
  it('puts the whole world in 256 pixels at zoom 0, which is what the map does', () => {
    expect(pixelsAt({ lat: 0, lon: -180 }, 0).x).toBeCloseTo(0, 6)
    expect(pixelsAt({ lat: 0, lon: 0 }, 0).x).toBeCloseTo(128, 6)
    expect(pixelsAt({ lat: 0, lon: 0 }, 0).y).toBeCloseTo(128, 6)
  })

  it('doubles the scale with every zoom level', () => {
    const here: Coordinate = { lat: 47.5, lon: -120.5 }
    expect(pixelsAt(here, 11).x).toBeCloseTo(pixelsAt(here, 10).x * 2, 6)
    expect(pixelsAt(here, 11).y).toBeCloseTo(pixelsAt(here, 10).y * 2, 6)
  })

  it('puts north above south, which a sign error would silently invert', () => {
    // Latitude runs the other way from y, and getting it backwards still produces plausible
    // distances — every cluster would just form with the wrong neighbours.
    expect(pixelsAt({ lat: 48, lon: -120 }, 10).y).toBeLessThan(
      pixelsAt({ lat: 47, lon: -120 }, 10).y,
    )
  })
})

describe('clusterPois', () => {
  it('leaves a lone place exactly where it is', () => {
    // A cluster of one is not a cluster. Averaging it with itself would be harmless here and
    // wrong the moment the arithmetic changes.
    const only = place('a', 47.5, -120.5)

    const clusters = clusterPois([only], { zoom: 10 })

    expect(clusters).toHaveLength(1)
    expect(clusters[0]?.members).toEqual([only])
    expect(clusters[0]?.coordinate).toEqual({ lat: 47.5, lon: -120.5 })
  })

  it('groups two pins that would overlap on screen', () => {
    const [near, alongside] = apartBy(CLUSTER_RADIUS_PX / 2, 10)

    const clusters = clusterPois([place('a', near.lat, near.lon), place('b', alongside.lat, alongside.lon)], {
      zoom: 10,
    })

    expect(clusters).toHaveLength(1)
    expect(clusters[0]?.members.map((member) => member.id)).toEqual(['a', 'b'])
  })

  it('leaves the same two alone once the rider has zoomed in on them', () => {
    // The whole reason this is screen space: zooming in is how a rider takes a cluster apart,
    // and it has to actually come apart.
    const [near, alongside] = apartBy(CLUSTER_RADIUS_PX / 2, 10)
    const pois = [place('a', near.lat, near.lon), place('b', alongside.lat, alongside.lon)]

    expect(clusterPois(pois, { zoom: 10 })).toHaveLength(1)
    expect(clusterPois(pois, { zoom: 14 })).toHaveLength(2)
  })

  it('keeps every place exactly once, however they fall', () => {
    // The property that matters more than any grouping decision: a rider must never lose a place
    // to the clusterer, and must never see one twice.
    const pois = [
      place('a', 47.5, -120.5),
      place('b', 47.5001, -120.5001),
      place('c', 47.9, -120.1),
      place('d', 47.9002, -120.1002),
      place('e', 48.4, -119.6),
    ]

    const clustered = clusterPois(pois, { zoom: 9 }).flatMap((cluster) => cluster.members)

    expect(clustered).toHaveLength(pois.length)
    expect(new Set(clustered.map((member) => member.id))).toEqual(
      new Set(pois.map((each) => each.id)),
    )
  })

  it('sits a group at the middle of its members', () => {
    const clusters = clusterPois([place('a', 47.5, -120.5), place('b', 47.5002, -120.5002)], {
      zoom: 10,
    })

    expect(clusters[0]?.coordinate.lat).toBeCloseTo(47.5001, 6)
    expect(clusters[0]?.coordinate.lon).toBeCloseTo(-120.5001, 6)
  })

  it('gives the same answer twice, so pins do not shuffle between renders', () => {
    const pois = [
      place('a', 47.5, -120.5),
      place('b', 47.5001, -120.5001),
      place('c', 47.9, -120.1),
    ]

    expect(clusterPois(pois, { zoom: 9 })).toEqual(clusterPois(pois, { zoom: 9 }))
  })

  it('names a group after its members rather than its position in the list', () => {
    // The key becomes a marker's identity. Keyed on an index, inserting one place at the front
    // rebuilds every marker after it — the churn that makes a map crawl over an editing session.
    const pair = [place('a', 47.5, -120.5), place('b', 47.5001, -120.5001)]
    const elsewhere = [place('c', 48.4, -119.6), place('d', 48.4001, -119.6001)]

    // The same two groups, in the other order, so the pair is no longer the first cluster out.
    const first = clusterPois([...pair, ...elsewhere], { zoom: 9 })
    const swapped = clusterPois([...elsewhere, ...pair], { zoom: 9 })

    const keyOfPair = (clusters: readonly { key: string; members: readonly Poi[] }[]) =>
      clusters.find((cluster) => cluster.members.some((member) => member.id === 'a'))?.key

    expect(keyOfPair(first)).toBe('a+b')
    expect(keyOfPair(swapped)).toBe(keyOfPair(first))
  })

  it('has nothing to say about an empty map', () => {
    expect(clusterPois([], { zoom: 10 })).toEqual([])
  })

  it('does not chain a line of pins into one blob', () => {
    // Greedy absorption around an anchor, not transitive merging. A string of places along a road
    // each a radius apart is a road, not a pile, and collapsing it to one pin would hide the
    // shape of the ride.
    const zoom = 10
    const [origin] = apartBy(0, zoom)
    const step = apartBy(CLUSTER_RADIUS_PX * 0.9, zoom)[1].lon - origin.lon
    const pois = Array.from({ length: 6 }, (_, index) =>
      place(`p${String(index)}`, origin.lat, origin.lon + step * index),
    )

    expect(clusterPois(pois, { zoom }).length).toBeGreaterThan(1)
  })
})
