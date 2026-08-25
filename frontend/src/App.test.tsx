import { render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { App } from './App'
import type { GoogleMaps } from './map/loadGoogleMaps'

/**
 * The shell, and the one rule it has to prove: **chat is an accelerator, never a
 * requirement.** Placing the start and end of a trip has to work with nothing but the
 * mouse, so these tests drive the map rather than the chat rail.
 */

const PIN_PROBE_ID = 'pin-probe'

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

/** The pin elements currently attached to the map. */
function attachedPins(fake: ReturnType<typeof createFakeMaps>): HTMLElement[] {
  return fake.markers
    .filter((marker) => marker.map !== null)
    .map((marker) => marker.options['content'] as HTMLElement)
}

/**
 * Waits for `expected` pins, then reads their names in route order.
 *
 * The wait and the DOM work are separate on purpose. `waitFor` retries whenever the
 * document mutates, so a polled callback that attaches nodes retriggers itself forever.
 * Only the settled result is put in the document, and into a probe node rather than
 * `document.body`, which holds the React root under test.
 */
async function pinLabels(
  fake: ReturnType<typeof createFakeMaps>,
  expected: number,
): Promise<string[]> {
  await waitFor(() => {
    expect(attachedPins(fake)).toHaveLength(expected)
  })

  const probe = document.getElementById(PIN_PROBE_ID) ?? document.createElement('div')
  probe.id = PIN_PROBE_ID
  document.body.append(probe)
  probe.replaceChildren(...attachedPins(fake))

  // Filtered by role: a pin the browser would treat as a generic div is not a pin.
  return within(probe)
    .queryAllByRole('img')
    .map((pin) => pin.title)
}

describe('App', () => {
  it('opens by telling the user both ways of starting', () => {
    const fake = createFakeMaps()

    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" />)

    expect(screen.getByText(/describe your trip/i)).toBeInTheDocument()
    expect(screen.getByText(/set a start and end point on the map/i)).toBeInTheDocument()
  })

  it('places the start and the end from map clicks alone', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    fake.clickMap(47.6, -120.7)
    expect(await pinLabels(fake, 1)).toEqual(['Start'])

    fake.clickMap(48.1, -120.2)
    expect(await pinLabels(fake, 2)).toEqual(['Start', 'End'])

    fake.clickMap(48.5, -119.9)
    expect(await pinLabels(fake, 3)).toEqual(['Start', 'Via point', 'End'])
  })

  it('reports the point count, so the map is not the only feedback', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    fake.clickMap(47.6, -120.7)

    expect(await screen.findByText(/1 point/i)).toBeInTheDocument()
  })

  it('offers an undo for a misplaced point, and only when there is one to undo', async () => {
    const fake = createFakeMaps()
    render(<App mapLoader={fake.loader} mapId="motorooter-test-vector" />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    expect(screen.queryByRole('button', { name: /remove last point/i })).not.toBeInTheDocument()

    fake.clickMap(47.6, -120.7)
    fake.clickMap(48.1, -120.2)
    expect(await pinLabels(fake, 2)).toHaveLength(2)

    screen.getByRole('button', { name: /remove last point/i }).click()

    expect(await pinLabels(fake, 1)).toEqual(['Start'])
  })
})
