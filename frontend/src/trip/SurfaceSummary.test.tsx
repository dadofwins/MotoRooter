import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SurfaceSummary } from './SurfaceSummary'
import type { Coordinate, RouteLeg, TripLeg } from '../api/types'
import { routeLeg } from '../api/fixtures'

/**
 * The surface breakdown in words.
 *
 * The map already draws three states; this is the half of the decision that was missing —
 * saying so in numbers, where a rider planning fuel and tyres will read it. The rule it
 * exists for is that unsurveyed distance never folds into paved.
 *
 * Identity is never carried by colour alone: every share is named in text next to its
 * swatch. That is not a nicety here, it is the same argument that made dirt dashed on the
 * map rather than merely orange.
 */

function leg(spans: RouteLeg['surface_spans'], distanceM: number): TripLeg {
  const geometry: Coordinate[] = Array.from({ length: 11 }, (_, index) => ({
    lat: 47 + index * 0.01,
    lon: -120,
  }))
  return {
    intent: 'unpaved',
    start_waypoint_index: 0,
    end_waypoint_index: 1,
    provider_override: null,
    routed: routeLeg({
      geometry,
      distance_m: distanceM,
      provider: 'ors',
      surface_spans: spans,
    }),
  }
}

/** Four tenths dirt, three tenths tarmac, three tenths untagged. */
const MIXED = [
  leg(
    [
      { start_index: 0, end_index: 4, surface: 'unpaved' },
      { start_index: 4, end_index: 7, surface: 'paved' },
    ],
    100_000,
  ),
]

describe('SurfaceSummary', () => {
  it('says nothing at all when there is no route', () => {
    const { container } = render(<SurfaceSummary legs={[]} unit="km" />)

    expect(container).toBeEmptyDOMElement()
  })

  it('names every share, so colour is never the only thing carrying it', () => {
    render(<SurfaceSummary legs={MIXED} unit="km" />)

    // Exact strings: /paved/i also matches "Unpaved", which is the sort of ambiguity that
    // makes a passing assertion mean nothing.
    expect(screen.getByText('Unpaved')).toBeInTheDocument()
    expect(screen.getByText('Paved')).toBeInTheDocument()
    // "Unsurveyed" rather than "unknown": it describes the data, not the road.
    expect(screen.getByText('Unsurveyed')).toBeInTheDocument()
  })

  it('reports shares that add up to 100', () => {
    render(<SurfaceSummary legs={MIXED} unit="km" />)

    expect(screen.getByText('40%')).toBeInTheDocument()
    // Two shares of 30% each, so both rows carry the same figure.
    expect(screen.getAllByText('30%')).toHaveLength(2)
  })

  it('gives the distance as well as the share, because a percentage is not a plan', () => {
    // 40% of 100 km. A rider deciding on tyres cares how far, not only how much.
    render(<SurfaceSummary legs={MIXED} unit="km" />)

    expect(screen.getByText(/40 km/)).toBeInTheDocument()
  })

  it('keeps unsurveyed distance visible rather than folding it into paved', () => {
    // The whole point. An untagged route must not read as tarmac.
    render(<SurfaceSummary legs={[leg([], 50_000)]} unit="km" />)

    expect(screen.getByText('100%')).toBeInTheDocument()
    expect(screen.getByText('Unsurveyed')).toBeInTheDocument()
    expect(screen.queryByText('Paved')).not.toBeInTheDocument()
  })

  it('leaves out a share that is genuinely zero', () => {
    // Nothing unsurveyed is worth saying nothing about; a 0% row is furniture.
    render(<SurfaceSummary legs={[leg([{ start_index: 0, end_index: 10, surface: 'paved' }], 20_000)]} unit="km" />)

    expect(screen.getByText('100%')).toBeInTheDocument()
    expect(screen.queryByText('Unsurveyed')).not.toBeInTheDocument()
  })

  it('puts the numbers in a list and leaves the bar decorative', () => {
    // The bar is a picture of the list. A screen reader that read both would say everything
    // twice, so only one of them is the accessible representation.
    render(<SurfaceSummary legs={MIXED} unit="km" />)

    expect(screen.getByRole('list')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(3)
  })
})
