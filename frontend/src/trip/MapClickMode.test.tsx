import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MapClickMode } from './MapClickMode'

/**
 * What a click on the map does.
 *
 * Tim: *"It's fine when you want to create a route manually but then when you're clicking on POIs
 * it's annoying if it keeps adding points to the route."* Clustering made this worse in a good
 * way — now that pins are findable and reachable, a rider spends far more time clicking the map
 * without wanting a waypoint out of it.
 *
 * A **mode** rather than a checkbox, because a checkbox names what a click does and a mode names
 * what the rider is doing. The risk in that word is real: this app already calls routing intent a
 * mode, and the two controls sit next to each other. It survives because they read as one
 * sentence — *add points, offroad* — rather than as two settings that happen to share a noun.
 */
function view(overrides: Partial<Parameters<typeof MapClickMode>[0]> = {}) {
  const onPlacingChange = vi.fn()
  const onIntentChange = vi.fn()
  render(
    <MapClickMode
      placing
      onPlacingChange={onPlacingChange}
      intent="unpaved"
      onIntentChange={onIntentChange}
      {...overrides}
    />,
  )
  return { onPlacingChange, onIntentChange }
}

describe('MapClickMode', () => {
  it('offers both states and says which one is on', () => {
    view({ placing: false })

    expect(screen.getByRole('radio', { name: /browse/i })).toBeChecked()
    expect(screen.getByRole('radio', { name: /add points/i })).not.toBeChecked()
  })

  it('switches to placing when the rider asks for it', () => {
    const { onPlacingChange } = view({ placing: false })

    fireEvent.click(screen.getByRole('radio', { name: /add points/i }))

    expect(onPlacingChange).toHaveBeenCalledWith(true)
  })

  it('switches back, which is the half he actually asked for', () => {
    const { onPlacingChange } = view({ placing: true })

    fireEvent.click(screen.getByRole('radio', { name: /browse/i }))

    expect(onPlacingChange).toHaveBeenCalledWith(false)
  })

  it('shows the mode new points will be routed with', () => {
    view({ intent: 'twisty_paved' })

    expect(screen.getByRole('combobox')).toHaveValue('twisty_paved')
  })

  it('changes that mode without going into a leg afterwards', () => {
    // The point of putting it here: the intent for a point is decidable *before* placing it,
    // rather than by opening the leg it created and correcting it.
    const { onIntentChange } = view()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'highway_connector' } })

    expect(onIntentChange).toHaveBeenCalledWith('highway_connector')
  })

  it('disables the mode while browsing, because it has nothing to apply to', () => {
    view({ placing: false })

    expect(screen.getByRole('combobox')).toBeDisabled()
  })

  it('names what the mode applies to, since it is not the whole trip', () => {
    // It seeds *new* segments. A rider who read it as the trip's mode would expect the legs they
    // already have to change, and they do not — each leg keeps its own.
    view()

    expect(screen.getByText(/new/i)).toBeInTheDocument()
  })

  it('offers the modes by the names Tim chose', () => {
    view()

    const options = within(screen.getByRole('combobox')).getAllByRole('option')
    expect(options.map((option) => option.textContent)).toEqual(['Fast', 'Twisties', 'Offroad'])
  })
})
