import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CategoryPicker } from './CategoryPicker'
import { DEFAULT_CATEGORIES } from './discoveryCategories'
import type { PoiCategory } from '../api/types'

/**
 * Choosing what a discovery run looks for.
 *
 * The last mouse-equivalence gap: `find_places` takes categories and the mouse could only say
 * "everything", so "find me more restaurants" worked by typing and not by clicking.
 *
 * Per category rather than per group, deliberately. Selecting by group would have left the
 * assistant able to ask for something the mouse still could not — the groups are here to organise
 * nine chips for the eye, not to be the unit of choice.
 *
 * And it is a cost control: discovery fans out one metered search per anchor per category, so
 * this is the difference between a cheap run and an expensive one. The UI says so, because a
 * rider who does not know that has no reason to narrow anything.
 */

function picker(overrides: Partial<Parameters<typeof CategoryPicker>[0]> = {}) {
  return (
    <CategoryPicker
      selected={DEFAULT_CATEGORIES}
      onChange={vi.fn()}
      disabled={false}
      {...overrides}
    />
  )
}

describe('CategoryPicker', () => {
  it('offers every category the app knows', () => {
    render(picker())

    // Nine, because a gap of one is still a gap: the assistant can ask for any of them.
    expect(screen.getAllByRole('checkbox')).toHaveLength(9)
  })

  it('organises them under the groups used everywhere else', () => {
    render(picker())

    expect(screen.getByText('Stays')).toBeInTheDocument()
    expect(screen.getByText('Supplies')).toBeInTheDocument()
    expect(screen.getByText('Sights')).toBeInTheDocument()
  })

  it('starts on the chosen default rather than on everything', () => {
    // The default is the decision, not the control: this is what almost every run will use.
    render(picker())

    expect(screen.getByRole('checkbox', { name: 'Wild camp' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Viewpoint' })).toBeChecked()
    // Off on purpose. A fuel station every 25 km is not information, and it is one of the
    // most expensive things to search for.
    expect(screen.getByRole('checkbox', { name: 'Fuel' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Food' })).not.toBeChecked()
  })

  it('adds a category the rider ticks', () => {
    const onChange = vi.fn<(next: readonly PoiCategory[]) => void>()
    render(picker({ selected: ['wild_camp'], onChange }))

    fireEvent.click(screen.getByRole('checkbox', { name: 'Food' }))

    expect(onChange).toHaveBeenCalledWith(['wild_camp', 'food'])
  })

  it('removes one the rider unticks', () => {
    const onChange = vi.fn<(next: readonly PoiCategory[]) => void>()
    render(picker({ selected: ['wild_camp', 'food'], onChange }))

    fireEvent.click(screen.getByRole('checkbox', { name: 'Food' }))

    expect(onChange).toHaveBeenCalledWith(['wild_camp'])
  })

  it('keeps the offered order rather than the click order', () => {
    // So the request, the list and the picker all read the same way round.
    const onChange = vi.fn<(next: readonly PoiCategory[]) => void>()
    render(picker({ selected: ['viewpoint'], onChange }))

    fireEvent.click(screen.getByRole('checkbox', { name: 'Wild camp' }))

    expect(onChange).toHaveBeenCalledWith(['wild_camp', 'viewpoint'])
  })

  it('will not let a rider search for nothing', () => {
    // A run with no categories finds nothing and still costs the route-search stage. Refusing
    // the last untick is kinder than letting them press a button that cannot work.
    const onChange = vi.fn()
    render(picker({ selected: ['wild_camp'], onChange }))

    fireEvent.click(screen.getByRole('checkbox', { name: 'Wild camp' }))

    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('checkbox', { name: 'Wild camp' })).toBeChecked()
  })

  it('says what the choice costs, because otherwise there is no reason to narrow it', () => {
    render(picker({ selected: ['wild_camp', 'campground'] }))

    expect(screen.getByText(/2 of 9/)).toBeInTheDocument()
    expect(screen.getByText(/fewer searches/i)).toBeInTheDocument()
  })

  it('does not claim a saving when everything is selected', () => {
    render(picker({ selected: ['wild_camp', 'campground', 'hotel', 'unique_stay', 'food', 'fuel', 'water', 'mechanic', 'viewpoint'] }))

    expect(screen.getByText(/9 of 9/)).toBeInTheDocument()
    expect(screen.queryByText(/fewer searches/i)).not.toBeInTheDocument()
  })

  it('locks while a run is going, because the run has already been priced', () => {
    render(picker({ disabled: true }))

    for (const box of screen.getAllByRole('checkbox')) expect(box).toBeDisabled()
  })
})
