import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RouteThroughBest } from './RouteThroughBest'
import { ApiError, ApiNetworkError } from '../api/errors'
import { poi, trip as tripFixture } from '../api/fixtures'
import type { RequestOptions } from '../api/client'
import type { Poi, RouteThroughBestRequest, RouteThroughBestResponse } from '../api/types'

/**
 * The mouse equivalent of "route through the ones that rank highly".
 *
 * The last affordance on Tim's list, and the one that keeps the feature from being chat-only. It
 * exists as its own action rather than a checkbox on Replan because the judgement is persisted —
 * `Poi.score` and the judge's sentence in `Poi.note` — so a rider can change their mind without
 * paying sixty seconds for discovery again.
 *
 * Three things it has to say, because it has no model to say them:
 *
 * **Why these.** The judge's own sentence, the same one the assistant quotes back.
 * **Why not the others.** A bound the rider cannot see reads as having found nothing else.
 * **How to undo it.** This edits the route on its own, which nothing else in the app does
 * without a click per point.
 */

function place(id: string, name: string, note: string | null = null): Poi {
  return poi({ id, name, note, source: 'places', place_id: `p-${id}`, score: 0.9 })
}

function response(overrides: Partial<RouteThroughBestResponse> = {}): RouteThroughBestResponse {
  return {
    trip: tripFixture({ slug: 'wabdr-north' }),
    added: [place('a', 'Miners', 'clearly rider-friendly with dedicated motorcycle parking')],
    left_out: [],
    ...overrides,
  }
}

function client(result: RouteThroughBestResponse = response()) {
  return {
    routeThroughBest: vi.fn(
      (_slug: string, _request: RouteThroughBestRequest, _options?: RequestOptions) =>
        Promise.resolve(result),
    ),
  }
}

function view(overrides: Partial<Parameters<typeof RouteThroughBest>[0]> = {}) {
  const api = client()
  render(
    <RouteThroughBest
      client={api}
      slug="wabdr-north"
      candidates={2}
      onRouted={vi.fn()}
      onUndo={vi.fn()}
      {...overrides}
    />,
  )
  return api
}

describe('RouteThroughBest', () => {
  it('offers nothing when there is nothing to route through', () => {
    // A button that would add nothing reads as an action that failed.
    view({ candidates: 0 })

    expect(screen.queryByRole('button', { name: /route through the best/i })).not.toBeInTheDocument()
  })

  it('asks for no particular number, so the backend picks the pace the ride implies', async () => {
    // The default is derived from the trip's length and shape. A number invented here would
    // override a decision made with more information than this component has.
    const api = view()

    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))

    await waitFor(() => {
      expect(api.routeThroughBest).toHaveBeenCalledWith('wabdr-north', {}, expect.anything())
    })
  })

  it('says which places it added and why each one', async () => {
    // The judge's own sentence. A rider clicking a button deserves the same reason the assistant
    // would have quoted at them.
    const api = client(
      response({
        added: [
          place('a', 'Miners', 'clearly rider-friendly with dedicated motorcycle parking'),
          place('b', 'Lone Fir', 'quiet dispersed camping just off the pass'),
        ],
      }),
    )
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={5} onRouted={vi.fn()} onUndo={vi.fn()} />,
    )

    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))

    expect(await screen.findByText(/dedicated motorcycle parking/)).toBeInTheDocument()
    expect(screen.getByText(/quiet dispersed camping/)).toBeInTheDocument()
  })

  it('says how many were good enough but did not fit', async () => {
    // A bound the rider cannot see reads as the search having found nothing else, which makes a
    // conservative default look like a poor result.
    const api = client(
      response({ left_out: [place('c', 'Halfway Flat'), place('d', 'Mineral Springs')] }),
    )
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={9} onRouted={vi.fn()} onUndo={vi.fn()} />,
    )

    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))

    expect(await screen.findByText(/2 more were good enough/i)).toBeInTheDocument()
  })

  it('offers to add more, which is the limit override by mouse', async () => {
    const api = client(response({ left_out: [place('c', 'Halfway Flat')] }))
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={9} onRouted={vi.fn()} onUndo={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))
    await screen.findByText(/1 more were good enough/i)

    fireEvent.click(screen.getByRole('button', { name: 'Add 1 more' }))

    // One already on, one left out — so ask for both rather than a number from nowhere.
    await waitFor(() => {
      expect(api.routeThroughBest).toHaveBeenLastCalledWith(
        'wabdr-north',
        { limit: 2 },
        expect.anything(),
      )
    })
  })

  it('says nothing about others when there were none', async () => {
    view()

    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))
    await screen.findByText(/motorcycle parking/)

    expect(screen.queryByText(/more/i)).not.toBeInTheDocument()
  })

  it('hands the saved trip back rather than making the caller re-read it', async () => {
    // The response carries the saved document precisely so the map can redraw without another
    // round trip.
    const onRouted = vi.fn()
    const saved = tripFixture({ slug: 'wabdr-north', name: 'Loop' })
    const api = client(response({ trip: saved }))
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={2} onRouted={onRouted} onUndo={vi.fn()} />,
    )

    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))

    await waitFor(() => {
      expect(onRouted).toHaveBeenCalledWith(saved)
    })
  })

  it('offers an undo, because nothing else edits the route on its own', async () => {
    // Every other route change in this app is one click per point. This one moves several at
    // once on the strength of a score the rider never saw, so it has to be reversible.
    const onUndo = vi.fn()
    const api = client()
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={2} onRouted={vi.fn()} onUndo={onUndo} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))
    await screen.findByText(/motorcycle parking/)

    fireEvent.click(screen.getByRole('button', { name: /undo/i }))

    expect(onUndo).toHaveBeenCalled()
  })

  it('clears the summary once the rider undoes it', async () => {
    // Leaving "added Miners" on screen after taking Miners off is worse than showing nothing.
    const api = client()
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={2} onRouted={vi.fn()} onUndo={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))
    await screen.findByText(/motorcycle parking/)

    fireEvent.click(screen.getByRole('button', { name: /undo/i }))

    await waitFor(() => {
      expect(screen.queryByText(/motorcycle parking/)).not.toBeInTheDocument()
    })
  })

  it('says plainly when it found nothing worth adding', async () => {
    // A real outcome on a short trip or a thin corridor, and silence would read as a broken
    // button.
    const api = client(response({ added: [], left_out: [] }))
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={3} onRouted={vi.fn()} onUndo={vi.fn()} />,
    )

    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))

    expect(await screen.findByText(/nothing.*worth|none of them/i)).toBeInTheDocument()
  })

  it('reports a failure without an internal string', async () => {
    const api = {
      routeThroughBest: vi.fn(() => Promise.reject(new ApiNetworkError({ detail: 'Failed to fetch' }))),
    }
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={2} onRouted={vi.fn()} onUndo={vi.fn()} />,
    )

    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/connection|reach/i)
  })

  it('does not fire twice while one is in flight', async () => {
    const api = {
      routeThroughBest: vi.fn(
        () =>
          new Promise<RouteThroughBestResponse>((resolve) => {
            setTimeout(() => resolve(response()), 20)
          }),
      ),
    }
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={2} onRouted={vi.fn()} onUndo={vi.fn()} />,
    )

    const button = screen.getByRole('button', { name: /route through the best/i })
    fireEvent.click(button)
    fireEvent.click(button)

    await waitFor(() => {
      expect(screen.queryByText(/motorcycle parking/)).toBeInTheDocument()
    })
    expect(api.routeThroughBest).toHaveBeenCalledTimes(1)
  })

  it('says when the trip has not been saved yet rather than failing generically', async () => {
    const api = {
      routeThroughBest: vi.fn(() =>
        Promise.reject(new ApiError({ status: 404, code: 'trip_not_found', detail: 'no such trip' })),
      ),
    }
    render(
      <RouteThroughBest client={api} slug="wabdr-north" candidates={2} onRouted={vi.fn()} onUndo={vi.fn()} />,
    )

    fireEvent.click(screen.getByRole('button', { name: /route through the best/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/no longer exists|saved/i)
  })
})
