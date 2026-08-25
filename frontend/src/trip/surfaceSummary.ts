/**
 * How much of a route is dirt, tarmac, and unsurveyed.
 *
 * The decision this honours: unknown surface stays unknown. On a real route roughly a third
 * of the distance carries no OSM surface tag, so "41% unpaved" on its own lets the remaining
 * 59% read as tarmac when a quarter of it is nobody's guess. Three numbers, and the third
 * does not get to disappear because it makes the chart tidier.
 *
 * Fractions are measured on the geometry, because the spans are the only place surface lives.
 * Distances are those fractions applied to the provider's own `distance_m`, so the summary
 * agrees with the total shown next to it instead of disagreeing by a percent and looking
 * broken.
 */
import type { Surface, TripLeg } from '../api/types'
import { distanceM } from '../routing/geo'

export interface SurfaceSummary {
  /** Share of the route by surface. Sums to 1. */
  readonly fractions: Record<Surface, number>
  /** Those shares in metres, against the provider's total. */
  readonly distanceM: Record<Surface, number>
  readonly totalM: number
}

const NONE: Record<Surface, number> = { paved: 0, unpaved: 0, unknown: 0 }

/**
 * Surface of each edge of a leg, defaulting to unknown.
 *
 * Spans are inclusive point ranges, so a span owns the edges *between* its points — the same
 * reading the map's own segment splitting uses, and the reason the two never disagree.
 */
function edgeSurfaces(leg: TripLeg): Surface[] {
  const geometry = leg.routed?.geometry ?? []
  const surfaces: Surface[] = Array.from({ length: Math.max(geometry.length - 1, 0) }, () => 'unknown')

  for (const span of leg.routed?.surface_spans ?? []) {
    const first = Math.max(span.start_index, 0)
    const last = Math.min(span.end_index, surfaces.length)
    for (let edge = first; edge < last; edge++) surfaces[edge] = span.surface
  }
  return surfaces
}

export function summariseSurface(legs: readonly TripLeg[]): SurfaceSummary | null {
  const measured: Record<Surface, number> = { ...NONE }
  let measuredTotal = 0
  let providerTotal = 0

  for (const leg of legs) {
    const geometry = leg.routed?.geometry ?? []
    if (geometry.length < 2) continue // no route yet: no length and no surface to report

    const surfaces = edgeSurfaces(leg)
    // Measured per leg, then scaled to that leg's own reported distance, so a short dirt
    // leg cannot outweigh a long paved one just by having more vertices.
    const legMeasured: Record<Surface, number> = { ...NONE }
    let legTotal = 0
    surfaces.forEach((surface, edge) => {
      const from = geometry[edge]
      const to = geometry[edge + 1]
      if (from === undefined || to === undefined) return
      const length = distanceM(from, to)
      legMeasured[surface] += length
      legTotal += length
    })
    if (legTotal === 0) continue

    const reported = leg.routed?.distance_m ?? legTotal
    for (const surface of ['paved', 'unpaved', 'unknown'] as const) {
      measured[surface] += (legMeasured[surface] / legTotal) * reported
    }
    measuredTotal += reported
    providerTotal += reported
  }

  if (measuredTotal === 0) return null

  return {
    fractions: {
      paved: measured.paved / measuredTotal,
      unpaved: measured.unpaved / measuredTotal,
      unknown: measured.unknown / measuredTotal,
    },
    distanceM: measured,
    totalM: providerTotal,
  }
}

/**
 * Percentages that add up to 100.
 *
 * Rounding each independently gives 33/33/33 or 34/33/34, and a reader who adds three
 * numbers to 99 has found a bug whether or not there is one. Largest remainder puts the
 * spare point on the share with the strongest claim to it.
 */
export function toWholePercentages(fractions: Record<Surface, number>): Record<Surface, number> {
  const order: Surface[] = ['unpaved', 'paved', 'unknown']
  const exact = order.map((surface) => fractions[surface] * 100)
  const floored = exact.map((value) => Math.floor(value))
  let spare = 100 - floored.reduce((total, value) => total + value, 0)

  const byRemainder = order
    .map((surface, index) => ({ surface, index, remainder: exact[index]! - floored[index]! }))
    .sort((a, b) => b.remainder - a.remainder)

  const result: Record<Surface, number> = { ...NONE }
  order.forEach((surface, index) => {
    result[surface] = floored[index] ?? 0
  })
  for (const { surface } of byRemainder) {
    if (spare <= 0) break
    result[surface] += 1
    spare -= 1
  }
  return result
}
