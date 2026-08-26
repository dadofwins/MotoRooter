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

  it('offers to route through a whole group, because a group is an itinerary and 29 places is not', () => {
    // Tim asked for "a button to route through found POIs". I did not build one button for all
    // of them: twenty-nine places is not an itinerary, it is a search result, and a control
    // nobody presses is the demo-shaped version of this feature. A group is how a rider thinks
    // about it — these are where I sleep, those are what I want to see — so the bulk action is
    // per group, and the count is in the label so the commitment is visible before the click.
    render(
      <PlaceList
        pois={[
          place({ id: 'a', name: 'Lone Fir', category: 'campground' }),
          place({ id: 'b', name: 'Halfway Flat', category: 'wild_camp' }),
          place({ id: 'c', name: 'Chevron', category: 'fuel' }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
        onRouteThrough={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: /route through 2 stays/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /route through 1 supply/i })).toBeInTheDocument()
  })

  it('hands over that group, in the order the list shows them', () => {
    const onRouteThrough = vi.fn()
    const lone = place({ id: 'a', name: 'Lone Fir', category: 'campground' })
    const flat = place({ id: 'b', name: 'Halfway Flat', category: 'wild_camp' })
    render(
      <PlaceList
        pois={[lone, flat, place({ id: 'c', name: 'Chevron', category: 'fuel' })]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
        onRouteThrough={onRouteThrough}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /route through 2 stays/i }))

    expect(onRouteThrough).toHaveBeenCalledWith([lone, flat])
  })

  it('leaves out the places already on the route, which are done', () => {
    // Counting them would offer to add what is already there, and the count is the whole point
    // of the label.
    render(
      <PlaceList
        pois={[
          place({ id: 'a', name: 'Lone Fir', category: 'campground', on_route: true }),
          place({ id: 'b', name: 'Halfway Flat', category: 'wild_camp' }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
        onRouteThrough={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: /route through 1 stay\b/i })).toBeInTheDocument()
  })

  it('leaves out the unconfirmed ones, which cannot be routed through at all', () => {
    render(
      <PlaceList
        pois={[
          poiFixture({ id: 'a', name: 'Maybe', category: 'campground', source: 'llm_suggested', place_id: null }),
          place({ id: 'b', name: 'Halfway Flat', category: 'wild_camp' }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
        onRouteThrough={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: /route through 1 stay\b/i })).toBeInTheDocument()
  })

  it('offers nothing for a group where everything is already decided', () => {
    // A button that would add nothing is worse than no button: it looks like the action failed.
    render(
      <PlaceList
        pois={[place({ id: 'a', name: 'Lone Fir', category: 'campground', on_route: true })]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
        onRouteThrough={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: /route through/i })).not.toBeInTheDocument()
  })

  it('offers no bulk action at all when the caller cannot take one', () => {
    render(
      <PlaceList
        pois={[place({ id: 'a', name: 'Lone Fir', category: 'campground' })]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: /route through/i })).not.toBeInTheDocument()
  })

  it('puts the better-judged places first', () => {
    // `Poi.score` is the judge's ranking key, and this is what it is for here. Displaying it as a
    // number would be meaningless without a scale — "0.90" tells a rider nothing — but ordering by
    // it means someone scanning twenty-nine places meets the good ones first, which is the same
    // use the backend makes of it when it caps the list.
    render(
      <PlaceList
        pois={[
          place({ id: 'a', name: 'Adequate', category: 'campground', score: 0.4 }),
          place({ id: 'b', name: 'Excellent', category: 'campground', score: 0.95 }),
          place({ id: 'c', name: 'Middling', category: 'campground', score: 0.7 }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    const names = screen.getAllByRole('listitem').map((row) => row.textContent ?? '')
    expect(names[0]).toMatch(/Excellent/)
    expect(names[1]).toMatch(/Middling/)
    expect(names[2]).toMatch(/Adequate/)
  })

  it('does not push an unjudged place to the bottom as though it scored zero', () => {
    // A place a rider added by hand has no score. Sorting it as zero would bury their own choice
    // beneath everything discovery found, which is the wrong way round.
    // Unscored *first* in the input, deliberately. With it second the expected order and the
    // input order coincide, so sorting a null as zero produces the same list and the test proves
    // nothing — which is exactly what a surviving mutation showed.
    render(
      <PlaceList
        pois={[
          place({ id: 'b', name: 'Mine', category: 'campground', score: null }),
          place({ id: 'a', name: 'Judged', category: 'campground', score: 0.5 }),
        ]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    // Kept in the order it arrived rather than sorted against a number it does not have.
    const names = screen.getAllByRole('listitem').map((row) => row.textContent ?? '')
    expect(names[0]).toMatch(/Mine/)
    expect(names[1]).toMatch(/Judged/)
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

describe('PlaceList marks', () => {
  it('leads a row with the pin that place has on the map', () => {
    // A bare glyph is not the pin: the pin is a coloured shape with a glyph in it, and matching
    // only the character leaves a rider with thirty rows translating prose into shapes.
    render(
      <PlaceList
        pois={[poiFixture({ id: 'a', name: 'Lone Fir', category: 'campground' })]}
        onOpen={vi.fn()}
        onIgnore={vi.fn()}
      />,
    )

    const mark = screen.getByRole('listitem').querySelector('.poi')
    expect(mark?.className).toContain('poi--stay')
    expect(mark?.getAttribute('aria-hidden')).toBe('true')
  })
})
