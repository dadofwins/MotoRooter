import { describe, expect, it } from 'vitest'
import { createMapOptions } from './mapOptions'

/**
 * The map is the product, so its options are design decisions rather than boilerplate and
 * are pinned here deliberately. Each assertion below is a choice someone could reasonably
 * reverse — the test is where the reasoning lives.
 */
describe('createMapOptions', () => {
  const base = { center: { lat: 47.6, lon: -120.7 }, zoom: 8, mapId: 'motorooter-vector' }

  it('renders vector, which is what the architecture picked and what advanced markers need', () => {
    const options = createMapOptions(base)

    expect(options.renderingType).toBe('VECTOR')
    expect(options.mapId).toBe('motorooter-vector')
  })

  it('converts lon to Google’s lng at the boundary, so the domain keeps its own naming', () => {
    const options = createMapOptions(base)

    expect(options.center).toEqual({ lat: 47.6, lng: -120.7 })
    expect(options.zoom).toBe(8)
  })

  it('pans on one finger — a rider on a phone should not have to fight the page', () => {
    expect(createMapOptions(base).gestureHandling).toBe('greedy')
  })

  it('makes Google’s own POI icons unclickable so they cannot steal a route click', () => {
    // Every click on this map means something to us: setting a point, or selecting a leg.
    // Google's built-in place icons would otherwise open their own info window on top.
    expect(createMapOptions(base).clickableIcons).toBe(false)
  })

  it('keeps the map-type control and drops the chrome that earns no space', () => {
    const options = createMapOptions(base)

    // Satellite is not decoration here: it is how you tell whether a track on the map is
    // a real road before committing to it on a loaded bike.
    expect(options.mapTypeControl).toBe(true)
    expect(options.streetViewControl).toBe(false)
    expect(options.fullscreenControl).toBe(false)
  })

  it('follows the system colour scheme by default, and can be pinned', () => {
    // Dark mode is not optional for a tool used outdoors and at night.
    expect(createMapOptions(base).colorScheme).toBe('FOLLOW_SYSTEM')
    expect(createMapOptions({ ...base, colorScheme: 'DARK' }).colorScheme).toBe('DARK')
  })

  it('omits mapId rather than sending an empty one when none is configured', () => {
    // An empty mapId is an error to the API, where an absent one falls back to raster.
    const options = createMapOptions({ ...base, mapId: '' })

    expect('mapId' in options).toBe(false)
  })
})
