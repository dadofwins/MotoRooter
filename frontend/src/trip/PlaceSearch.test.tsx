import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PlaceSearch } from './PlaceSearch'
import { ApiError, ApiNetworkError, ApiNotImplementedError } from '../api/errors'
import type { RequestOptions } from '../api/client'
import type { Coordinate, GeocodeResponse } from '../api/types'

/**
 * Typing a place name instead of hunting for it on the map.
 *
 * This closes the half of Tim's original trip-creation spec that never shipped — *"type a
 * starting and ending address or choose to click on the map"* — because geocoding did not exist
 * until now. It is older than tonight's feedback.
 *
 * **Ambiguity is a refusal, not a best guess.** The endpoint returns a list and chooses nothing,
 * and neither does this: taking the first result silently would be wrong the first time somebody
 * types a name that exists twice, which is the case the list exists for. Backend's own live test
 * hit it immediately — "Stevens Pass" matched two places and the assistant had to read the
 * address to pick.
 */

function result(overrides: Partial<GeocodeResponse['results'][number]> = {}) {
  return {
    name: 'Leavenworth',
    place_id: 'ChIJ123',
    coordinate: { lat: 47.5962, lon: -120.6615 },
    address: 'Leavenworth, WA 98826, USA',
    kinds: ['locality'],
    ...overrides,
  }
}

function client(results: GeocodeResponse['results']) {
  return {
    geocode: vi.fn(
      (_query: string, _options?: RequestOptions & { readonly near?: Coordinate }) =>
        Promise.resolve({ results }),
    ),
  }
}

async function search(text: string): Promise<void> {
  fireEvent.change(screen.getByRole('searchbox', { name: /add a place by name/i }), {
    target: { value: text },
  })
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }))
  await Promise.resolve()
}

describe('PlaceSearch', () => {
  it('looks up what the rider typed', async () => {
    const api = client([result()])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    await search('leavenworth')

    await waitFor(() => {
      expect(api.geocode).toHaveBeenCalledWith('leavenworth', expect.anything())
    })
  })

  it('biases toward where the trip already is', async () => {
    // What makes "Leavenworth" the Washington one rather than the Kansas one.
    const api = client([result()])
    render(<PlaceSearch client={api} near={{ lat: 47.5, lon: -120.4 }} onChoose={vi.fn()} />)

    await search('leavenworth')

    await waitFor(() => {
      expect(api.geocode.mock.calls[0]?.[1]?.near).toEqual({ lat: 47.5, lon: -120.4 })
    })
  })

  it('sends no bias when the trip has nowhere to bias toward', async () => {
    const api = client([result()])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    await search('leavenworth')

    await waitFor(() => {
      expect(api.geocode.mock.calls[0]?.[1]?.near).toBeUndefined()
    })
  })

  it('shows the address, which is what makes two of the same name choosable', async () => {
    // The reason the field exists. Backend's live test hit it on the first run: "Stevens Pass"
    // matched two places, and the addresses are the only thing telling them apart.
    const api = client([
      result({ name: 'Stevens Pass', place_id: 'a', address: 'US-2, Skykomish, WA 98288, USA' }),
      result({ name: 'Stevens', place_id: 'b', address: 'Stevens County, WA, USA' }),
    ])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    await search('stevens pass')

    expect(await screen.findByText(/Skykomish/)).toBeInTheDocument()
    expect(screen.getByText(/Stevens County/)).toBeInTheDocument()
  })

  it('shows the name Places uses, not what was typed', async () => {
    // Otherwise somebody searching "woodinville" wonders whether they got what they asked for.
    const api = client([result({ name: 'Woodinville', address: 'Woodinville, WA, USA' })])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    await search('woodinville wa')

    expect(await screen.findByRole('button', { name: /Woodinville, WA, USA/ })).toBeInTheDocument()
  })

  it('chooses nothing on the rider’s behalf, however few results there are', async () => {
    // One result is still a claim. Adding it silently is the shortcut that is wrong the first
    // time a single match is the wrong place.
    const onChoose = vi.fn()
    const api = client([result()])
    render(<PlaceSearch client={api} near={null} onChoose={onChoose} />)

    await search('leavenworth')
    await screen.findByText(/Leavenworth, WA/)

    expect(onChoose).not.toHaveBeenCalled()
  })

  it('hands over the place the rider picked', async () => {
    const onChoose = vi.fn()
    const chosen = result({ name: 'Stevens Pass', place_id: 'a', address: 'US-2, Skykomish, WA' })
    const api = client([chosen, result({ name: 'Stevens', place_id: 'b' })])
    render(<PlaceSearch client={api} near={null} onChoose={onChoose} />)

    await search('stevens')
    fireEvent.click(await screen.findByRole('button', { name: /Skykomish/ }))

    expect(onChoose).toHaveBeenCalledWith(chosen)
  })

  it('clears the results once one is chosen', async () => {
    // The list has done its job. Leaving it up invites a second click that adds a second point.
    const api = client([result()])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)
    await search('leavenworth')

    fireEvent.click(await screen.findByRole('button', { name: /Leavenworth, WA/ }))

    await waitFor(() => {
      expect(screen.queryByText(/Leavenworth, WA 98826/)).not.toBeInTheDocument()
    })
  })

  it('says nothing matched rather than showing a failure', async () => {
    // An empty list is an ordinary answer to a typo, and a 200 with nothing in it is correct on
    // both sides. Treating it as an error would blame the rider for a spelling mistake.
    const api = client([])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    await search('asdfgh')

    expect(await screen.findByText(/no places found/i)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('will not search for nothing', () => {
    const api = client([result()])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /^search$/i }))

    expect(api.geocode).not.toHaveBeenCalled()
  })

  it('marks what kind of place each result is', () => {
    // Places' own types, which backend put there for exactly this. The address disambiguates; a
    // glyph makes a list of five scannable without reading every address.
    const api = client([
      result({ name: 'Leavenworth', place_id: 'a', kinds: ['locality'] }),
      result({ name: 'Stevens Pass', place_id: 'b', kinds: ['natural_feature'] }),
    ])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    return search('stevens').then(async () => {
      const rows = await screen.findAllByRole('listitem')
      expect(rows[0]?.textContent ?? '').toMatch(/town/i)
      expect(rows[1]?.textContent ?? '').toMatch(/landmark|feature/i)
    })
  })

  it('calls an unfamiliar kind something generic rather than nothing', () => {
    // The list is Google's and it grows. An unrecognised value must not produce a blank column
    // or, worse, a guess.
    const api = client([result({ kinds: ['plus_code', 'point_of_interest'] })])
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    return search('somewhere').then(async () => {
      const row = await screen.findByRole('listitem')
      expect(row.textContent ?? '').toMatch(/place/i)
    })
  })

  it('reads a 501 as not built yet rather than as a failure', async () => {
    const api = {
      geocode: vi.fn(() =>
        Promise.reject(new ApiNotImplementedError({ detail: 'no places credentials' })),
      ),
    }
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    await search('leavenworth')

    expect(await screen.findByText(/not built yet/i)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('reports a real failure without an internal string', async () => {
    const api = {
      geocode: vi.fn(() => Promise.reject(new ApiNetworkError({ detail: 'Failed to fetch' }))),
    }
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    await search('leavenworth')

    expect(await screen.findByRole('alert')).toHaveTextContent(/connection|reach/i)
  })

  it('says when the search was refused for going too fast', async () => {
    const api = {
      geocode: vi.fn(() =>
        Promise.reject(new ApiError({ status: 429, code: 'rate_limited', detail: 'slow down' })),
      ),
    }
    render(<PlaceSearch client={api} near={null} onChoose={vi.fn()} />)

    await search('leavenworth')

    expect(await screen.findByRole('alert')).toHaveTextContent(/too many|moment/i)
  })
})
