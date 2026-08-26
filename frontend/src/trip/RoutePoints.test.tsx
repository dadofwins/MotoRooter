import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RoutePoints } from './RoutePoints'
import { waypoint } from '../api/fixtures'

/**
 * The route as a list, and the reason it exists.
 *
 * Chat is an accelerator, never a requirement: if the assistant gets `remove_waypoint`, the
 * mouse needs the same reach. It had "Remove last point" and nothing else, so taking a
 * via-point out of the *middle* of a route was something only the assistant could do — which
 * would have made chat the only way to do it, the exact inversion the rule forbids.
 *
 * A list rather than only right-click-the-pin. Right-click is the fast path and it is
 * undiscoverable; a list is visible, keyboard-reachable, and gives the segments between the
 * points somewhere to live when they get their own mode control.
 */

describe('RoutePoints', () => {
  it('lists the points in route order', () => {
    render(
      <RoutePoints
        waypoints={[
          waypoint(47.6, -122.1, { name: 'Woodinville' }),
          waypoint(47.5, -120.4, { name: 'Cashmere' }),
          waypoint(46.9, -120.5, { name: 'Ellensburg' }),
        ]}
        onRemove={vi.fn()}
      />,
    )

    expect(screen.getAllByRole('listitem').map((row) => row.textContent)).toEqual([
      expect.stringContaining('Woodinville'),
      expect.stringContaining('Cashmere'),
      expect.stringContaining('Ellensburg'),
    ])
  })

  it('falls back to coordinates for a point nobody has named', () => {
    // Every point placed by clicking the map is unnamed until forward geocoding exists. Showing
    // nothing would make the row unidentifiable, which defeats the point of a list.
    render(<RoutePoints waypoints={[waypoint(47.5215, -120.4685)]} onRemove={vi.fn()} />)

    expect(screen.getByRole('listitem').textContent).toMatch(/47\.5215/)
    expect(screen.getByRole('listitem').textContent).toMatch(/-120\.4685/)
  })

  it('names each remove control by the point it removes', () => {
    // "×" three times over is unusable with a screen reader, and ambiguous with a mouse once
    // the list is longer than the rail.
    render(
      <RoutePoints
        waypoints={[waypoint(47.6, -122.1, { name: 'Woodinville' }), waypoint(47.5, -120.4)]}
        onRemove={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: 'Remove Woodinville' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove point 2' })).toBeInTheDocument()
  })

  it('removes the point the rider chose, not the last one', () => {
    // The whole reason for the branch. `withWaypointRemoved` has always accepted any index; the
    // affordance was what was missing.
    const onRemove = vi.fn()
    render(
      <RoutePoints
        waypoints={[
          waypoint(47.6, -122.1, { name: 'Woodinville' }),
          waypoint(47.5, -120.4, { name: 'Cashmere' }),
          waypoint(46.9, -120.5, { name: 'Ellensburg' }),
        ]}
        onRemove={onRemove}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Remove Cashmere' }))

    expect(onRemove).toHaveBeenCalledWith(1)
  })

  it('shows nothing at all when there is no route', () => {
    // An empty list with a heading reads as something being broken rather than absent.
    render(<RoutePoints waypoints={[]} onRemove={vi.fn()} />)

    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('does not annotate every row when they are all the same', () => {
    // Every point a rider clicks is pinned, so on a route nobody has replanned the label was on
    // all five rows and distinguished nothing. Found by rendering the rail and looking at it.
    render(
      <RoutePoints
        waypoints={[
          waypoint(47.6, -122.1, { name: 'Woodinville', pinned: true }),
          waypoint(47.5, -120.4, { name: 'Cashmere', pinned: true }),
        ]}
        onRemove={vi.fn()}
      />,
    )

    expect(screen.queryByText(/placed by you/i)).not.toBeInTheDocument()
  })

  it('says which points a rider placed by hand', () => {
    // A replan may move or drop an unpinned point but must leave a pinned one alone, so the
    // difference is worth seeing before pressing Replan rather than after.
    render(
      <RoutePoints
        waypoints={[
          waypoint(47.6, -122.1, { name: 'Woodinville', pinned: true }),
          waypoint(47.5, -120.4, { name: 'Suggested stop', pinned: false }),
        ]}
        onRemove={vi.fn()}
      />,
    )

    const rows = screen.getAllByRole('listitem')
    expect(rows[0]?.textContent).toMatch(/placed by you/i)
    expect(rows[1]?.textContent).not.toMatch(/placed by you/i)
  })
})
