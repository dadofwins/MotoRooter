import { describe, expect, it, vi } from 'vitest'
import { gpxFilename, saveBlob } from './gpx'

/**
 * Getting a GPX file onto a rider's computer.
 *
 * Two things kept apart from React on purpose. The filename is pure string work with a
 * surprising number of ways to produce something a filesystem refuses, and the save itself is
 * the only part of this app that reaches for browser APIs jsdom does not implement — so it takes
 * an injectable sink rather than being untestable.
 */

describe('gpxFilename', () => {
  it('uses the name the rider gave the trip', () => {
    expect(gpxFilename('WABDR North', 'trip-abc123')).toBe('wabdr-north.gpx')
  })

  it('falls back to the slug when there is no name worth using', () => {
    // A trip created by clicking twice is called "Untitled trip" or nothing at all; the slug is
    // at least unique, which is what matters once four of these are in a downloads folder.
    expect(gpxFilename('', 'trip-abc123')).toBe('trip-abc123.gpx')
    expect(gpxFilename('   ', 'trip-abc123')).toBe('trip-abc123.gpx')
  })

  it('strips what a filesystem or a GPS unit would refuse', () => {
    // Path separators are the one that matters: a name is rider-supplied and world-editable by
    // link, so "../../etc/passwd" must not become a path.
    expect(gpxFilename('../../etc/passwd', 'trip-1')).toBe('etc-passwd.gpx')
    expect(gpxFilename('Trip: Sept 3rd / 4th?', 'trip-1')).toBe('trip-sept-3rd-4th.gpx')
  })

  it('keeps letters and digits from other alphabets', () => {
    // Sanitising by allowlisting ASCII would silently rename a whole trip to its slug.
    expect(gpxFilename('Väg 63 Åre', 'trip-1')).toBe('väg-63-åre.gpx')
  })

  it('collapses runs of separators rather than leaving them', () => {
    expect(gpxFilename('North   ---   South', 'trip-1')).toBe('north-south.gpx')
  })

  it('shortens a name nothing could store', () => {
    const long = 'a'.repeat(300)

    const filename = gpxFilename(long, 'trip-1')

    // Comfortably inside the 255-byte limit every common filesystem shares, extension included.
    expect(filename.length).toBeLessThanOrEqual(100)
    expect(filename.endsWith('.gpx')).toBe(true)
  })

  it('does not end up with a name that is only punctuation', () => {
    // "???" sanitises to nothing, and a file called ".gpx" is hidden on Unix and confusing
    // everywhere else.
    expect(gpxFilename('???', 'trip-1')).toBe('trip-1.gpx')
  })
})

describe('saveBlob', () => {
  function fakeSink() {
    const clicked: { url: string; filename: string }[] = []
    const revoked: string[] = []
    return {
      clicked,
      revoked,
      sink: {
        createUrl: (blob: Blob) => `blob:${String(blob.size)}`,
        revokeUrl: (url: string) => revoked.push(url),
        click: (url: string, filename: string) => clicked.push({ url, filename }),
      },
    }
  }

  it('hands the browser a named download', () => {
    const { sink, clicked } = fakeSink()

    saveBlob(new Blob(['<gpx/>']), 'wabdr-north.gpx', sink)

    expect(clicked).toEqual([{ url: 'blob:6', filename: 'wabdr-north.gpx' }])
  })

  it('releases the object URL afterwards', () => {
    // An object URL pins its blob in memory until the document goes away. A rider exporting
    // repeatedly while planning would hold every version of a 10,000-point track.
    const { sink, revoked } = fakeSink()

    saveBlob(new Blob(['<gpx/>']), 'a.gpx', sink)

    expect(revoked).toEqual(['blob:6'])
  })

  it('releases it even when the click throws', () => {
    const revoked: string[] = []
    const sink = {
      createUrl: () => 'blob:x',
      revokeUrl: (url: string) => revoked.push(url),
      click: () => {
        throw new Error('no')
      },
    }

    expect(() => saveBlob(new Blob(['x']), 'a.gpx', sink)).toThrow()
    expect(revoked).toEqual(['blob:x'])
  })

  it('is a no-op nobody has to guard when there is no browser to save into', () => {
    // Server-side rendering is not something this app does, but a component calling it during a
    // test should not need a jsdom shim for an API jsdom lacks.
    const create = vi.fn()

    expect(() =>
      saveBlob(new Blob(['x']), 'a.gpx', {
        createUrl: create as unknown as (blob: Blob) => string,
        revokeUrl: () => undefined,
        click: () => undefined,
      }),
    ).not.toThrow()
  })
})
