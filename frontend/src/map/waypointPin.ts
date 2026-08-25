/**
 * Waypoint markers.
 *
 * `AdvancedMarkerElement` takes a DOM element as its content, so a pin is plain markup:
 * styled in `index.css`, testable without the Maps API, and free of the `PinElement`
 * helper's constraints.
 */

export type WaypointKind = 'start' | 'via' | 'end'

export interface WaypointPinInput {
  readonly kind: WaypointKind
  /** Short text inside the pin — the waypoint's position in the route. */
  readonly label: string
  /** The user's name for the place, if it has one. */
  readonly name?: string
}

const ACCESSIBLE_KIND: Record<WaypointKind, string> = {
  start: 'Start',
  via: 'Via point',
  end: 'End',
}

/** Which end of the route a waypoint sits at. */
export function waypointKind(index: number, total: number): WaypointKind {
  if (index === 0) return 'start'
  // A single point is a start with no end yet, not both at once.
  if (index === total - 1) return 'end'
  return 'via'
}

export function createWaypointPin(input: WaypointPinInput): HTMLElement {
  const pin = document.createElement('div')
  pin.className = `pin pin--${input.kind}`
  pin.textContent = input.label

  const kindText = ACCESSIBLE_KIND[input.kind]
  // The shape and colour carry this information visually; the label carries it otherwise.
  pin.setAttribute('aria-label', input.name === undefined ? kindText : `${kindText}: ${input.name}`)
  return pin
}
