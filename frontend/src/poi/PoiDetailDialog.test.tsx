import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PoiDetailDialog } from './PoiDetailDialog'
import { ApiError, ApiNetworkError, ApiNotImplementedError } from '../api/errors'
import type { RequestOptions } from '../api/client'
import type { Poi, PoiDetailResponse } from '../api/types'

/**
 * The place detail dialog.
 *
 * Three constraints shape it, and none of them is about layout.
 *
 * Google's terms allow storing `place_id` and essentially nothing else, so everything the
 * dialog fetches lives and dies with it. Nothing is cached, which is asserted here by
 * reopening and expecting a second request.
 *
 * `GET /api/places/{place_id}` answers 501 today, and that is "not built yet", not a failure.
 * The client raises a distinct error for it precisely so this can be told apart.
 *
 * And the case that matters most to this app — dispersed camping — is the case Places knows
 * least about. A place with a name, a coordinate and nothing else is the *normal* result, so
 * it has to be a designed state rather than an empty shell where a rating should be.
 */

function poi(overrides: Partial<Poi> = {}): Poi {
  return {
    id: 'poi-1',
    name: 'Lone Fir Campground',
    category: 'campground',
    coordinate: { lat: 48.512_34, lon: -120.678_91 },
    source: 'places',
    place_id: 'ChIJ123',
    note: null,
    on_route: false,
    ...overrides,
  }
}

function detail(overrides: Partial<PoiDetailResponse['detail']> = {}): PoiDetailResponse {
  return {
    detail: {
      poi: poi(),
      rating: 4.6,
      user_rating_count: 812,
      photo_urls: [],
      opening_hours: [],
      reviews: [],
      phone: null,
      website: null,
      ...overrides,
    },
  }
}

function fakeClient(response: PoiDetailResponse = detail()) {
  return {
    placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) => Promise.resolve(response)),
  }
}

describe('PoiDetailDialog', () => {
  it('is a dialog, named after the place', async () => {
    render(<PoiDetailDialog poi={poi()} client={fakeClient()} onClose={vi.fn()} />)

    const dialog = await screen.findByRole('dialog', { name: /Lone Fir Campground/ })
    expect(dialog).toBeInTheDocument()
  })

  it('shows what is already known before anything is fetched', async () => {
    // The name and category come from the POI itself. Waiting on Places to show them would
    // make a click feel unanswered.
    const client = {
      placeDetail: vi.fn(
        (_placeId: string, _options?: RequestOptions) => new Promise<PoiDetailResponse>(() => undefined),
      ),
    }

    render(<PoiDetailDialog poi={poi()} client={client} onClose={vi.fn()} />)

    expect(screen.getByText('Lone Fir Campground')).toBeInTheDocument()
    expect(screen.getByText('Campground')).toBeInTheDocument() // the category, not the name
    expect(await screen.findByRole('status')).toBeInTheDocument()
  })

  it('shows the listing details once they arrive', async () => {
    const client = fakeClient(
      detail({
        rating: 4.6,
        user_rating_count: 812,
        phone: '+1 509-555-0100',
        website: 'https://example.test/lone-fir',
        opening_hours: ['Mon: 08:00 – 20:00'],
      }),
    )

    render(<PoiDetailDialog poi={poi()} client={client} onClose={vi.fn()} />)

    expect(await screen.findByText(/4\.6/)).toBeInTheDocument()
    expect(screen.getByText(/812/)).toBeInTheDocument()
    expect(screen.getByText('Mon: 08:00 – 20:00')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /website/i })).toHaveAttribute(
      'href',
      'https://example.test/lone-fir',
    )
    expect(screen.getByRole('link', { name: /509-555-0100/ })).toBeInTheDocument()
  })

  it('makes a place with no listing data a proper state, not an empty shell', async () => {
    // Dispersed camping is the case this app exists for and the case Places knows least
    // about. The rider still gets the thing they can actually use: coordinates.
    const client = fakeClient(detail({ rating: null, user_rating_count: null }))

    render(
      <PoiDetailDialog
        poi={poi({ name: 'Pull-out above Harts Pass', note: 'Flat, room for two tents' })}
        client={client}
        onClose={vi.fn()}
      />,
    )

    await waitFor(() => expect(client.placeDetail).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/no listing details/i)).toBeInTheDocument()
    // No empty rating furniture where a number should be.
    expect(screen.queryByText(/★/)).not.toBeInTheDocument()
    // What is genuinely useful: the note discovery wrote, and coordinates for a GPS.
    expect(screen.getByText(/Flat, room for two tents/)).toBeInTheDocument()
    expect(screen.getByText(/48\.51234/)).toBeInTheDocument()
    expect(screen.getByText(/-120\.67891/)).toBeInTheDocument()
  })

  it('treats the 501 stub as not built yet, not as a failure', async () => {
    const client = {
      placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) =>
        Promise.reject(new ApiNotImplementedError({ detail: 'Places enrichment is not implemented yet' })),
      ),
    }

    render(<PoiDetailDialog poi={poi()} client={client} onClose={vi.fn()} />)

    expect(await screen.findByText(/not available yet/i)).toBeInTheDocument()
    // Not an error: nothing is broken and there is nothing for the rider to do.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('reports a real failure as a failure', async () => {
    const client = {
      placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) =>
        Promise.reject(new ApiNetworkError({ detail: 'Failed to fetch' })),
      ),
    }

    render(<PoiDetailDialog poi={poi()} client={client} onClose={vi.fn()} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/connection|reach/i)
  })

  it('never shows an internal error string', async () => {
    const client = {
      placeDetail: vi.fn((_placeId: string, _options?: RequestOptions) =>
        Promise.reject(
          new ApiError({ status: 502, code: 'provider_unavailable', detail: '[places] upstream 503' }),
        ),
      ),
    }

    render(<PoiDetailDialog poi={poi()} client={client} onClose={vi.fn()} />)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toContain('places')
    expect(alert.textContent).not.toContain('upstream')
  })

  it('asks nothing of Places for a POI that has no place_id', async () => {
    // There is nothing to ask about: an unresolved suggestion has no listing to look up.
    const client = fakeClient()

    render(
      <PoiDetailDialog
        poi={poi({ source: 'llm_suggested', place_id: null })}
        client={client}
        onClose={vi.fn()}
      />,
    )

    expect(await screen.findByText(/not been confirmed/i)).toBeInTheDocument()
    expect(client.placeDetail).not.toHaveBeenCalled()
  })

  it('offers to add a confirmed place to the route, and does not for a suggestion', async () => {
    const onAddToRoute = vi.fn()
    const { unmount } = render(
      <PoiDetailDialog poi={poi()} client={fakeClient()} onClose={vi.fn()} onAddToRoute={onAddToRoute} />,
    )

    fireEvent.click(await screen.findByRole('button', { name: /add to route/i }))
    expect(onAddToRoute).toHaveBeenCalledTimes(1)
    unmount()

    render(
      <PoiDetailDialog
        poi={poi({ source: 'llm_suggested', place_id: null })}
        client={fakeClient()}
        onClose={vi.fn()}
        onAddToRoute={onAddToRoute}
      />,
    )
    expect(screen.queryByRole('button', { name: /add to route/i })).not.toBeInTheDocument()
  })

  it('closes on the button and on Escape', async () => {
    const onClose = vi.fn()
    render(<PoiDetailDialog poi={poi()} client={fakeClient()} onClose={onClose} />)

    fireEvent.click(await screen.findByRole('button', { name: /close/i }))
    expect(onClose).toHaveBeenCalledTimes(1)

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('holds nothing between openings, because the terms do not allow it', async () => {
    // Google's terms permit storing place_id and essentially nothing else. A second opening
    // asking again is the observable consequence of not keeping the first answer.
    const client = fakeClient()
    const { unmount } = render(<PoiDetailDialog poi={poi()} client={client} onClose={vi.fn()} />)
    await waitFor(() => expect(client.placeDetail).toHaveBeenCalledTimes(1))
    unmount()

    render(<PoiDetailDialog poi={poi()} client={client} onClose={vi.fn()} />)

    await waitFor(() => expect(client.placeDetail).toHaveBeenCalledTimes(2))
  })

  it('abandons the request when closed before it lands', async () => {
    const signals: AbortSignal[] = []
    const client = {
      placeDetail: vi.fn((_placeId: string, options?: RequestOptions) => {
        if (options?.signal !== undefined) signals.push(options.signal)
        return new Promise<PoiDetailResponse>(() => undefined)
      }),
    }

    const { unmount } = render(<PoiDetailDialog poi={poi()} client={client} onClose={vi.fn()} />)
    await waitFor(() => expect(signals).toHaveLength(1))
    unmount()

    expect(signals[0]?.aborted).toBe(true)
  })

  it('shows photos with alt text, and none when there are none', async () => {
    const withPhotos = fakeClient(detail({ photo_urls: ['https://example.test/a.jpg'] }))

    const { unmount } = render(
      <PoiDetailDialog poi={poi()} client={withPhotos} onClose={vi.fn()} />,
    )
    expect(await screen.findByRole('img', { name: /Lone Fir Campground/ })).toBeInTheDocument()
    unmount()

    render(<PoiDetailDialog poi={poi()} client={fakeClient()} onClose={vi.fn()} />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })
})
