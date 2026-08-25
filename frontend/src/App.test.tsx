import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { App } from './App'
import type { GoogleMaps } from './map/loadGoogleMaps'

/**
 * The shell, and the one rule it has to prove: **chat is an accelerator, never a
 * requirement.** Placing the start and end of a trip has to work with nothing but the
 * mouse, so these tests drive the map rather than the chat rail.
 */

interface FakeMarker {
  readonly options: Record<string, unknown>
  /** Set to null when the marker is detached, which is how a removed pin disappears. */
  map: unknown
}

function createFakeMaps() {
  const markers: FakeMarker[] = []
  let clickHandler: ((event: unknown) => void) | null = null

  class FakeMap {
    addListener(event: string, handler: (event: unknown) => void): { remove: () => void } {
      if (event === 'click') clickHandler = handler
      return { remove: () => undefined }
    }
    fitBounds(): void {
      // Nothing to assert here; framing is covered in MapCanvas's own tests.
    }
  }

  const namespace = {
    Map: FakeMap,
    Polyline: class {
      setMap(): void {}
    },
    LatLngBounds: class {
      extend(): this {
        return this
      }
    },
    marker: {
      AdvancedMarkerElement: class implements FakeMarker {
        map: unknown
        constructor(readonly options: Record<string, unknown>) {
          this.map = options['map'] ?? null
          markers.push(this)
        }
      },
    },
  }

  return {
    loader: () => Promise.resolve(namespace as unknown as GoogleMaps),
    markers,
    clickMap(lat: number, lon: number): void {
      if (clickHandler === null) throw new Error('the map has no click listener')
      clickHandler({ latLng: { lat: () => lat, lng: () => lon } })
    },
  }
}

/** Accessible names of the pins currently on the map. */
function pinLabels(fake: ReturnType<typeof createFakeMaps>): (string | null)[] {
  return fake.markers
    .filter((marker) => marker.map !== null)
    .map((marker) => (marker.options['content'] as HTMLElement).getAttribute('aria-label'))
}

describe('App', () => {
  it('opens by telling the user both ways of starting', () => {
    const fake = createFakeMaps()

    render(<App mapLoader={fake.loader} />)

    expect(screen.getByText(/describe your trip/i)).toBeInTheDocument()
    expect(screen.getByText(/set a start and end point on the map/i)).toBeInTheDocument()
  })

  it('places the start and the end from map clicks alone', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    fake.clickMap(47.6, -120.7)
    await waitFor(() => expect(pinLabels(fake)).toEqual(['Start']))

    fake.clickMap(48.1, -120.2)
    await waitFor(() => expect(pinLabels(fake)).toEqual(['Start', 'End']))

    fake.clickMap(48.5, -119.9)
    await waitFor(() => expect(pinLabels(fake)).toEqual(['Start', 'Via point', 'End']))
  })

  it('reports the point count, so the map is not the only feedback', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    fake.clickMap(47.6, -120.7)

    expect(await screen.findByText(/1 point/i)).toBeInTheDocument()
  })

  it('offers an undo for a misplaced point, and only when there is one to undo', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    expect(screen.queryByRole('button', { name: /remove last point/i })).not.toBeInTheDocument()

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    await waitFor(() => expect(pinLabels(fake)).toHaveLength(2))

    screen.getByRole('button', { name: /remove last point/i }).click()

    await waitFor(() => expect(pinLabels(fake)).toEqual(['Start']))
  })
})
