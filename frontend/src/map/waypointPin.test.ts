import { screen } from '@testing-library/dom'
import { describe, expect, it } from 'vitest'
import { createWaypointPin, waypointKind } from './waypointPin'

/**
 * Advanced markers take a DOM element, which means the pin is ordinary markup and can be
 * tested without the Maps API. Worth doing: a rider glancing at the map has to tell where
 * the route starts from where it ends, and a screen reader user has to get the same
 * information the shape conveys.
 */
describe('createWaypointPin', () => {
  it('distinguishes start, via and end visually', () => {
    const classes = (['start', 'via', 'end'] as const).map(
      (kind) => createWaypointPin({ kind, label: '1' }).className,
    )

    expect(new Set(classes).size).toBe(3)
  })

  it('shows the label it was given', () => {
    expect(createWaypointPin({ kind: 'via', label: '3' }).textContent).toContain('3')
  })

  it('names itself for a screen reader, including any user-given place name', () => {
    // Queried by role and *computed accessible name*, not by reading the attribute back.
    // ARIA prohibits aria-label on a generic element, so a bare div with the attribute set
    // passes an attribute check while a screen reader announces nothing at all.
    document.body.append(
      createWaypointPin({ kind: 'start', label: '1' }),
      createWaypointPin({ kind: 'end', label: '4', name: 'Sun Mountain Lodge' }),
    )

    expect(screen.getByRole('img', { name: 'Start' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'End: Sun Mountain Lodge' })).toBeInTheDocument()
  })

  it('carries a name the map itself can use as a tooltip', () => {
    // AdvancedMarkerElement takes a `title`; without one, an unnamed waypoint hovers blank.
    expect(createWaypointPin({ kind: 'via', label: '2' }).title).toBe('Via point')
  })
})

describe('waypointKind', () => {
  it('labels the first and last points of a route', () => {
    expect(waypointKind(0, 3)).toBe('start')
    expect(waypointKind(1, 3)).toBe('via')
    expect(waypointKind(2, 3)).toBe('end')
  })

  it('treats a lone first point as the start, not as the end', () => {
    // Setting the start on an empty map is the first thing a user does with the mouse.
    // Until a second point exists there is no end, and calling it one would be a lie.
    expect(waypointKind(0, 1)).toBe('start')
  })
})
