import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { GpxExport } from './GpxExport'
import { ApiError, ApiNetworkError, ApiNotImplementedError } from '../api/errors'
import type { RequestOptions } from '../api/client'
import type { DownloadSink } from './gpx'

/**
 * Getting the trip onto a GPS unit.
 *
 * A download, not a preview: nothing here needs to render a GPX, the file goes to a device. So
 * the tests are about the two things a rider can be misled by — what they are getting, and what
 * happened when they did not get it.
 *
 * The thing worth saying before the click is the decimation. A GPX carries a track, Garmin units
 * have point-count limits, and a long route therefore arrives thinned. A rider who was told will
 * forgive it; one who finds out on the trail will not.
 */

function sink(): { sink: DownloadSink; saved: { url: string; filename: string }[] } {
  const saved: { url: string; filename: string }[] = []
  return {
    saved,
    sink: {
      createUrl: () => 'blob:test',
      revokeUrl: () => undefined,
      click: (url, filename) => saved.push({ url, filename }),
    },
  }
}

function exporter(blob: Blob = new Blob(['<gpx/>'])) {
  return { exportGpx: vi.fn((_slug: string, _options?: RequestOptions) => Promise.resolve(blob)) }
}

function view(overrides: Partial<Parameters<typeof GpxExport>[0]> = {}) {
  const download = sink()
  const client = exporter()
  render(
    <GpxExport
      client={client}
      slug="wabdr-north"
      tripName="WABDR North"
      waypointCount={4}
      placeCount={7}
      sink={download.sink}
      {...overrides}
    />,
  )
  return { client, ...download }
}

describe('GpxExport', () => {
  it('says what the file will contain before the rider commits to it', () => {
    view()

    expect(screen.getByText(/7 places/i)).toBeInTheDocument()
  })

  it('warns that a long track is thinned, which is the surprise worth avoiding', () => {
    // Backend owns the decimation; saying so is this side's job. Finding out on the trail that
    // a route was thinned is a much worse way to learn it.
    view()

    expect(screen.getByText(/thinned|point limit/i)).toBeInTheDocument()
  })

  it('downloads under the trip name rather than the slug', async () => {
    const { saved } = view()

    fireEvent.click(screen.getByRole('button', { name: /download gpx/i }))

    await waitFor(() => expect(saved).toHaveLength(1))
    expect(saved[0]?.filename).toBe('wabdr-north.gpx')
  })

  it('asks for the trip it was given', async () => {
    const { client, saved } = view()

    fireEvent.click(screen.getByRole('button', { name: /download gpx/i }))

    await waitFor(() => expect(saved).toHaveLength(1))
    expect(client.exportGpx).toHaveBeenCalledWith('wabdr-north', expect.anything())
  })

  it('cannot export a trip with nowhere to go', () => {
    // Same shape as the Replan button under two points: a GPX of one point is not a route, and
    // a button that produces a useless file is worse than one that declines.
    view({ waypointCount: 1 })

    expect(screen.getByRole('button', { name: /download gpx/i })).toBeDisabled()
  })

  it('says it is working, because a big trip takes a moment', async () => {
    const client = {
      exportGpx: vi.fn(
        () =>
          new Promise<Blob>((resolve) => {
            setTimeout(() => resolve(new Blob(['<gpx/>'])), 20)
          }),
      ),
    }
    const download = sink()
    render(
      <GpxExport
        client={client}
        slug="wabdr-north"
        tripName="WABDR North"
        waypointCount={4}
        placeCount={0}
        sink={download.sink}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /download gpx/i }))

    expect(await screen.findByText(/preparing/i)).toBeInTheDocument()
    await waitFor(() => expect(download.saved).toHaveLength(1))
  })

  it('presents the 501 stub as not built yet, not as a failure', async () => {
    // It answers 501 today. Showing that in red teaches a rider to distrust the control once it
    // starts working.
    const client = {
      exportGpx: vi.fn(() =>
        Promise.reject(new ApiNotImplementedError({ detail: 'gpx export is not implemented yet' })),
      ),
    }
    const download = sink()
    render(
      <GpxExport
        client={client}
        slug="wabdr-north"
        tripName="WABDR North"
        waypointCount={4}
        placeCount={0}
        sink={download.sink}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /download gpx/i }))

    expect(await screen.findByText(/not built yet|coming soon/i)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('reports a real failure as one, without an internal string', async () => {
    const client = {
      exportGpx: vi.fn(() => Promise.reject(new ApiNetworkError({ detail: 'Failed to fetch' }))),
    }
    const download = sink()
    render(
      <GpxExport
        client={client}
        slug="wabdr-north"
        tripName="WABDR North"
        waypointCount={4}
        placeCount={0}
        sink={download.sink}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /download gpx/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/connection|reach/i)
    expect(download.saved).toHaveLength(0)
  })

  it('says a trip nobody has saved cannot be exported', async () => {
    // The export is addressed by slug and reads the stored document, so an unsaved edit is not
    // in the file. A 404 is the honest signal for that rather than a generic failure.
    const client = {
      exportGpx: vi.fn(() =>
        Promise.reject(new ApiError({ status: 404, code: 'trip_not_found', detail: 'no such trip' })),
      ),
    }
    const download = sink()
    render(
      <GpxExport
        client={client}
        slug="wabdr-north"
        tripName="WABDR North"
        waypointCount={4}
        placeCount={0}
        sink={download.sink}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /download gpx/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/saved|not found/i)
  })

  it('does not fire a second request while one is in flight', async () => {
    const client = {
      exportGpx: vi.fn(
        () =>
          new Promise<Blob>((resolve) => {
            setTimeout(() => resolve(new Blob(['<gpx/>'])), 20)
          }),
      ),
    }
    const download = sink()
    render(
      <GpxExport
        client={client}
        slug="wabdr-north"
        tripName="WABDR North"
        waypointCount={4}
        placeCount={0}
        sink={download.sink}
      />,
    )

    const button = screen.getByRole('button', { name: /download gpx/i })
    fireEvent.click(button)
    fireEvent.click(button)

    await waitFor(() => expect(download.saved).toHaveLength(1))
    expect(client.exportGpx).toHaveBeenCalledTimes(1)
  })
})
