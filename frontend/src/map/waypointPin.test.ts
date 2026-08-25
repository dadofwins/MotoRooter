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
    const unnamed = createWaypointPin({ kind: 'start', label: '1' })
    const named = createWaypointPin({ kind: 'end', label: '4', name: 'Sun Mountain Lodge' })

    expect(unnamed.getAttribute('aria-label')).toBe('Start')
    expect(named.getAttribute('aria-label')).toBe('End: Sun Mountain Lodge')
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
