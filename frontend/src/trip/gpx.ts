/**
 * Getting a GPX file onto a rider's computer.
 *
 * Kept out of React because both halves are awkward in their own way. The filename is pure
 * string work with a surprising number of ways to produce something a filesystem refuses — and
 * trip names are rider-supplied and world-editable by link, so they are untrusted input. The save
 * is the only place this app reaches for browser APIs jsdom does not implement, so it takes an
 * injectable sink rather than being untestable.
 *
 * Why fetch-then-save rather than pointing the browser at the URL and letting the server set the
 * filename: the endpoint still answers 501, and a navigation would render raw JSON in a tab
 * instead of a message. Downloading through the client keeps every error state we already have,
 * at the cost of owning the filename — which is not a guess, it is the only option once the
 * response is a `Blob`.
 */

/** The browser bits, injectable so the save is testable and so nothing here needs a jsdom shim. */
export interface DownloadSink {
  readonly createUrl: (blob: Blob) => string
  readonly revokeUrl: (url: string) => void
  readonly click: (url: string, filename: string) => void
}

/**
 * Characters a name may keep.
 *
 * An allowlist, because the alternative — blocking known-bad characters — misses whatever the
 * next filesystem objects to. Letters and digits from *any* alphabet are kept: allowlisting ASCII
 * would silently rename a Swedish or Japanese trip to its slug.
 */
const KEEP = /[\p{L}\p{N}]+/gu

/**
 * Long enough for any real trip name, short enough that no filesystem refuses it.
 *
 * The common limit is 255 *bytes*, and a name of accented or CJK characters costs two to four
 * bytes each — so a generous character budget can still overflow. 96 characters is under the
 * limit even at four bytes apiece.
 */
const MAX_STEM = 96

/**
 * A filename for the trip: its name where there is one, its slug otherwise.
 *
 * The name is friendlier — four files called `trip-abc123.gpx` in a downloads folder are useless —
 * but it is also untrusted, so it is rebuilt from the characters worth keeping rather than
 * filtered. That makes path traversal structurally impossible instead of blocked: there is no
 * separator to escape with, because separators are never copied through.
 */
export function gpxFilename(name: string, slug: string): string {
  const stem = (name.match(KEEP) ?? []).join('-').toLowerCase().slice(0, MAX_STEM)
  // A name of nothing but punctuation sanitises to nothing, and `.gpx` alone is hidden on Unix
  // and meaningless everywhere.
  return `${stem === '' ? slug : stem}.gpx`
}

const BROWSER_SINK: DownloadSink = {
  createUrl: (blob) => URL.createObjectURL(blob),
  revokeUrl: (url) => {
    URL.revokeObjectURL(url)
  },
  click: (url, filename) => {
    const anchor = document.createElement('a')
    anchor.href = url
    // The attribute is what makes this a download rather than a navigation, and it is where the
    // filename comes from now that the response is a blob rather than a redirect.
    anchor.download = filename
    anchor.rel = 'noopener'
    document.body.append(anchor)
    anchor.click()
    anchor.remove()
  },
}

/**
 * Save a blob under a name.
 *
 * The object URL is always released: it pins its blob in memory until the document goes away, and
 * a rider exporting repeatedly while planning would otherwise hold every version of a
 * ten-thousand-point track.
 */
export function saveBlob(blob: Blob, filename: string, sink: DownloadSink = BROWSER_SINK): void {
  const url = sink.createUrl(blob)
  try {
    sink.click(url, filename)
  } finally {
    sink.revokeUrl(url)
  }
}
