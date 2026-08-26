import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PlaceList } from './PlaceList'
import { poi as poiFixture } from '../api/fixtures'

/**
 * The discovered places, as a list.
 *
 * This exists because of what Tim said after a successful discovery run: *"I don't see any to
 * click on"* — of twenty-nine places that had been found, resolved and judged. They were pins,
 * and he was right anyway. Twenty-nine pins on a map is a haystack, and the rail is where a
 * rider decides.
 *
 * So the list's job is *triage*: enough to tell one place from another without a Places call,
 * grouped so the eye can find the kind of thing it is looking for, and one click to the detail.
 */

function place(overrides: Partial<Parameters<typeof poiFixture>[0]> = {}) {
  return poiFixture({ source: 'places', place_id: 'place-1', ...overrides })
}

describe('PlaceList', () => {
  it('shows nothing at all before anything has been found', () => {
    // An empty heading reads as a broken panel rather than as a run not yet made.
    render(<PlaceList pois={[]} onOpen={vi.fn()} onIgnore={vi.fn()} />)

    expect(screen.queryByRole('region', { name: /places/i })).not.toBeInTheDocument()
  })

  it('lists what was found, by name', () => {
    render(
      <PlaceList
        pois={[
          place({ id: 'a', name: 'Lone Fir Campground', category: 'campground' }),
          place({ id: 'b', name: 'Blewett Pass Viewpoint', category: 'viewpoint' }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    // Anchored: an unanchored name also matches "Ignore Lone Fir Campground", so the loose
    // version found two buttons and would have found the wrong one first.
    expect(screen.getByRole('button', { name: /^Lone Fir Campground/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Blewett Pass Viewpoint/ })).toBeInTheDocument()
  })

  it('groups them so a rider can find the kind of thing they want', () => {
    // Twenty-nine ungrouped rows is the same haystack the map was. The groups are the ones the
    // pins already use, so the list and the map agree about what a thing is.
    render(
      <PlaceList
        pois={[
          place({ id: 'a', name: 'Lone Fir', category: 'campground' }),
          place({ id: 'b', name: 'Chevron', category: 'fuel' }),
          place({ id: 'c', name: 'The Overlook', category: 'viewpoint' }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { name: /stays/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /supplies/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /sights/i })).toBeInTheDocument()
  })

  it('leaves out a group nothing was found for', () => {
    render(
      <PlaceList
        pois={[place({ id: 'a', name: 'Lone Fir', category: 'campground' })]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { name: /stays/i })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /supplies/i })).not.toBeInTheDocument()
  })

  it('says what kind of place each row is', () => {
    render(
      <PlaceList
        pois={[place({ id: 'a', name: 'Lone Fir', category: 'wild_camp' })]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    expect(screen.getByRole('listitem').textContent).toMatch(/wild camp/i)
  })

  it('carries the discovery note, which is the only signal it has for free', () => {
    // `Poi.note` is the judge's own reason for keeping the place. Everything else worth
    // knowing costs a Places call, and Google's terms forbid keeping the answer — so this is
    // what triage has to run on.
    render(
      <PlaceList
        pois={[place({ id: 'a', name: 'Lone Fir', note: 'Free dispersed sites by the river' })]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    expect(screen.getByText(/Free dispersed sites by the river/)).toBeInTheDocument()
  })

  it('marks the places already on the route, which are decided', () => {
    render(
      <PlaceList
        pois={[
          place({ id: 'a', name: 'Lone Fir', on_route: true }),
          place({ id: 'b', name: 'Somewhere Else', on_route: false }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    const rows = screen.getAllByRole('listitem')
    expect(rows.find((row) => row.textContent?.includes('Lone Fir'))?.textContent).toMatch(
      /on the route/i,
    )
    expect(rows.find((row) => row.textContent?.includes('Somewhere Else'))?.textContent).not.toMatch(
      /on the route/i,
    )
  })

  it('opens the detail for the place the rider clicked', () => {
    const onOpen = vi.fn()
    const lone = place({ id: 'a', name: 'Lone Fir' })
    render(<PlaceList pois={[lone, place({ id: 'b', name: 'Other' })]} onOpen={onOpen} onIgnore={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /^Lone Fir/ }))

    expect(onOpen).toHaveBeenCalledWith(lone)
  })

  it('ignores the place the rider chose, named so the control is not a bare cross', () => {
    const onIgnore = vi.fn()
    const lone = place({ id: 'a', name: 'Lone Fir' })
    render(<PlaceList pois={[lone]} onOpen={vi.fn()} onIgnore={onIgnore} />)

    fireEvent.click(screen.getByRole('button', { name: 'Ignore Lone Fir' }))

    expect(onIgnore).toHaveBeenCalledWith(lone)
  })

  it('says which places are unconfirmed, rather than letting them look the same', () => {
    // An LLM suggestion that never resolved to a place_id cannot be pinned to a route, so a
    // rider should know that before opening it rather than finding a missing button.
    render(
      <PlaceList
        pois={[
          poiFixture({ id: 'a', name: 'Maybe Camp', source: 'llm_suggested', place_id: null }),
          place({ id: 'b', name: 'Real Camp' }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    const rows = screen.getAllByRole('listitem')
    expect(rows.find((row) => row.textContent?.includes('Maybe Camp'))?.textContent).toMatch(
      /unconfirmed/i,
    )
    expect(rows.find((row) => row.textContent?.includes('Real Camp'))?.textContent).not.toMatch(
      /unconfirmed/i,
    )
  })

  it('counts what it is showing, because twenty-nine was the number nobody could see', () => {
    render(
      <PlaceList
        pois={[place({ id: 'a', name: 'One' }), place({ id: 'b', name: 'Two' })]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    expect(within(screen.getByRole('region', { name: /places/i })).getByText(/2 places/i)).toBeInTheDocument()
  })
})
