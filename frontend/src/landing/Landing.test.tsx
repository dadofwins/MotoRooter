import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Landing } from './Landing'

/**
 * The front door.
 *
 * A rider arriving cold needs somewhere to start, which is why this reverses the earlier
 * "no dialog, just place a point" decision — that was right for an existing trip and wrong as
 * an entrance. Click-to-create still works once you are on the map.
 *
 * The other job is being honest about what the trip list is: a per-browser record of slugs
 * visited, not an account. Clearing it loses the list, not the trips, and the links still work.
 */

describe('Landing', () => {
  it('offers a way to start a trip', () => {
    render(<Landing trips={[]} onCreate={vi.fn()} onOpen={vi.fn()} onForget={vi.fn()} />)

    expect(screen.getByRole('button', { name: /start a new trip/i })).toBeInTheDocument()
  })

  it('names the trip it creates, because a list of "New trip" is not a list', () => {
    const onCreate = vi.fn()
    render(<Landing trips={[]} onCreate={onCreate} onOpen={vi.fn()} onForget={vi.fn()} />)

    fireEvent.change(screen.getByRole('textbox', { name: /trip name/i }), {
      target: { value: 'WABDR North' },
    })
    fireEvent.click(screen.getByRole('button', { name: /start a new trip/i }))

    expect(onCreate).toHaveBeenCalledWith('WABDR North')
  })

  it('starts a trip without a name rather than blocking on the form', () => {
    // The earlier decision still holds underneath: naming is an offer, not a toll.
    const onCreate = vi.fn()
    render(<Landing trips={[]} onCreate={onCreate} onOpen={vi.fn()} onForget={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /start a new trip/i }))

    expect(onCreate).toHaveBeenCalledWith('')
  })

  it('lists the trips this browser has seen, most recent first', () => {
    render(
      <Landing
        trips={[
          { slug: 'newer', name: 'Cascades loop' },
          { slug: 'older', name: 'WABDR North' },
        ]}
        onCreate={vi.fn()}
        onOpen={vi.fn()}
        onForget={vi.fn()}
      />,
    )

    const names = screen.getAllByRole('listitem').map((row) => row.textContent ?? '')
    expect(names[0]).toContain('Cascades loop')
    expect(names[1]).toContain('WABDR North')
  })

  it('opens one when asked', () => {
    const onOpen = vi.fn()
    render(
      <Landing
        trips={[{ slug: 'wabdr-north', name: 'WABDR North' }]}
        onCreate={vi.fn()}
        onOpen={onOpen}
        onForget={vi.fn()}
      />,
    )

    // Exact: the remove button's label also contains the trip name, and a regex here would
    // match both — an ambiguity that makes a passing assertion mean nothing.
    fireEvent.click(screen.getByRole('button', { name: 'WABDR North' }))

    expect(onOpen).toHaveBeenCalledWith('wabdr-north')
  })

  it('says the list is per-browser, and that removing a trip is not deleting it', () => {
    // A rider who clears their browser has lost a list, not their trips. Saying so is the
    // difference between a shrug and a panic.
    render(
      <Landing
        trips={[{ slug: 'wabdr-north', name: 'WABDR North' }]}
        onCreate={vi.fn()}
        onOpen={vi.fn()}
        onForget={vi.fn()}
      />,
    )

    expect(screen.getByText(/this browser/i)).toBeInTheDocument()
  })

  it('removes a trip from the list without pretending to delete it', () => {
    const onForget = vi.fn()
    render(
      <Landing
        trips={[{ slug: 'wabdr-north', name: 'WABDR North' }]}
        onCreate={vi.fn()}
        onOpen={vi.fn()}
        onForget={onForget}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /remove WABDR North from this list/i }))

    expect(onForget).toHaveBeenCalledWith('wabdr-north')
  })

  it('says nothing about a list that is empty', () => {
    render(<Landing trips={[]} onCreate={vi.fn()} onOpen={vi.fn()} onForget={vi.fn()} />)

    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('submits on Enter, because a one-field form should not need the mouse', () => {
    const onCreate = vi.fn()
    render(<Landing trips={[]} onCreate={onCreate} onOpen={vi.fn()} onForget={vi.fn()} />)

    const field = screen.getByRole('textbox', { name: /trip name/i })
    fireEvent.change(field, { target: { value: 'Cascades loop' } })
    fireEvent.submit(field.closest('form') as HTMLFormElement)

    expect(onCreate).toHaveBeenCalledWith('Cascades loop')
  })
})
