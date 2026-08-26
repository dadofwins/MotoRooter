import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { LegModePicker } from './LegModePicker'

/**
 * Choosing how one segment routes.
 *
 * The second mouse-equivalence gap: `set_leg_intent` is one of the assistant's tools and there
 * was no per-leg control at all, so chat would have been the only way to change a routing mode.
 *
 * The rule that shapes it: **do not hardcode which modes report surface.** A hand-kept list went
 * stale the day the policy table repointed an intent and produced an entirely grey route, so the
 * answer is resolved through `GET /api/routing/capabilities` and told to the rider *at the moment
 * of choosing* — after the fact it is just an unexplained grey line.
 */

function picker(overrides: Partial<Parameters<typeof LegModePicker>[0]> = {}) {
  return (
    <LegModePicker
      legIndex={0}
      intent="unpaved"
      from="Cashmere"
      to="Blewett Pass"
      reportsSurface={() => true}
      reportsTrustworthyDuration={() => true}
      onChange={vi.fn()}
      {...overrides}
    />
  )
}

describe('LegModePicker', () => {
  it('names the segment it governs, so a rider knows which one they are changing', () => {
    render(picker())

    expect(screen.getByRole('combobox', { name: /Cashmere to Blewett Pass/i })).toBeInTheDocument()
  })

  it('offers the three modes by their rider-facing names', () => {
    render(picker())

    const labels = screen.getAllByRole('option').map((option) => option.textContent)
    expect(labels).toEqual(['Fast', 'Twisties', 'Offroad'])
  })

  it('shows the mode the leg is actually on', () => {
    render(picker({ intent: 'twisty_paved' }))

    expect(screen.getByRole('combobox')).toHaveValue('twisty_paved')
  })

  it('still shows a mode that has no rider-facing label yet', () => {
    // `technical_offroad` is a real mode with no name agreed. A blank select would read as a bug,
    // and silently showing "Offroad" would be a lie about what the leg is doing.
    render(picker({ intent: 'technical_offroad' }))

    expect(screen.getByRole('combobox')).toHaveValue('technical_offroad')
    expect(screen.getByRole('option', { name: 'technical_offroad' })).toBeInTheDocument()
  })

  it('reports the leg and the mode chosen', () => {
    const onChange = vi.fn()
    render(picker({ legIndex: 2, onChange }))

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'highway_connector' } })

    expect(onChange).toHaveBeenCalledWith(2, 'highway_connector')
  })

  it('warns that a mode costs the surface breakdown, before it is chosen', () => {
    // Measured live: Google returns zero spans, so 229 of 269 km of a real trip rendered grey.
    // A rider who picks Fast for a long connector should know that is what they are buying.
    render(picker({ intent: 'highway_connector', reportsSurface: (intent) => intent === 'unpaved' }))

    // Matched exactly rather than loosely: /surface/i also hits the option labels, which made
    // the assertion pass on the wrong element.
    expect(screen.getByText(/no dirt or paved breakdown/i)).toBeInTheDocument()
  })

  it('says nothing about surface when the mode does report it', () => {
    render(picker({ intent: 'unpaved', reportsSurface: () => true }))

    expect(screen.queryByText(/no dirt or paved breakdown/i)).not.toBeInTheDocument()
  })

  it('does not warn on an answer it does not have', () => {
    // Null is "the table has not said", which is not the same as "this mode will not tell you".
    // Warning on it would cry wolf on every render before the capabilities land.
    render(picker({ intent: 'highway_connector', reportsSurface: () => null }))

    expect(screen.queryByText(/no dirt or paved breakdown/i)).not.toBeInTheDocument()
  })

  it('marks the modes that cost the breakdown in the list itself', () => {
    // Told at the moment of choosing, which means in the options — a warning that only appears
    // after the choice is a warning about a decision already made.
    render(picker({ reportsSurface: (intent) => intent === 'unpaved' }))

    expect(screen.getByRole('option', { name: /Fast.*no surface/i })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Offroad' })).toBeInTheDocument()
  })

  it('says whose riding time a mode gives you, without calling it unreliable', () => {
    // Provenance, not quality — and the distinction matters because the two engines fail in
    // opposite directions. On dirt *our* number is the better one: ORS reported 143 min for a
    // 40 km leg that takes 46. A rider must not come away thinking the dirt leg is the dodgy one.
    render(picker({ intent: 'unpaved', reportsTrustworthyDuration: () => false }))

    const note = screen.getByText(/riding time/i).textContent ?? ''
    expect(note).toMatch(/our (own )?estimate/i)
    expect(note).not.toMatch(/unreliable|inaccurate|cannot be trusted/i)
  })

  it('says nothing about time when the engine figure is the one used', () => {
    render(picker({ intent: 'highway_connector', reportsTrustworthyDuration: () => true }))

    expect(screen.queryByText(/riding time/i)).not.toBeInTheDocument()
  })

  it('does not caveat a figure before the table has said anything', () => {
    render(picker({ intent: 'unpaved', reportsTrustworthyDuration: () => null }))

    expect(screen.queryByText(/riding time/i)).not.toBeInTheDocument()
  })

  it('leaves the time note out of the options, because it is not a cost', () => {
    // The surface warning *is* in the option labels: choosing Fast loses you the dirt/paved
    // breakdown, which is a loss at the moment of choosing. Whose clock produced the estimate is
    // not a loss, so putting it in every label would read as nine warnings.
    render(picker({ reportsTrustworthyDuration: () => false, reportsSurface: () => true }))

    for (const option of screen.getAllByRole('option')) {
      expect(option.textContent ?? '').not.toMatch(/riding time|estimate/i)
    }
  })
})
