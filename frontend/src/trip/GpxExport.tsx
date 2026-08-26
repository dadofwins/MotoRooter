/**
 * The trip as a file for a GPS unit.
 *
 * A download, not a preview: nothing here needs to render a GPX, and a rider who wanted to look
 * at one has a map already. So the component's whole job is to say what they are getting, get it,
 * and be clear about what happened when it does not arrive.
 *
 * **The decimation is the thing worth saying before the click.** A GPX carries a track, Garmin
 * units have point-count limits, and a route of ten thousand points therefore arrives thinned.
 * Backend owns the thinning; saying so is this side's job, because a rider who was told will
 * forgive it and one who finds out on the trail will not.
 *
 * The endpoint declares `application/gpx+xml` and takes no parameters, so what travels is the
 * stored document — which is also why ignoring a place removes it from the export: it is already
 * out of the document rather than merely hidden.
 */
import { useCallback, useRef, useState } from 'react'
import { saveBlob, gpxFilename, type DownloadSink } from './gpx'
import { isApiError, isNotImplemented } from '../api/errors'
import { routeErrorMessage } from './routeErrorMessage'
import type { ApiClient } from '../api/client'

export type GpxExporter = Pick<ApiClient, 'exportGpx'>

export interface GpxExportProps {
  readonly client: GpxExporter
  /** Null before anything has been saved: there is no document to export yet. */
  readonly slug: string | null
  readonly tripName: string | null
  readonly waypointCount: number
  /** How many discovered places are on the trip, and so in the file as waypoints. */
  readonly placeCount: number
  /** Injectable so the save is testable — jsdom implements no object URLs. */
  readonly sink?: DownloadSink
}

type Status = 'idle' | 'working' | 'unavailable' | 'failed'

/**
 * The export's own reading of a failure.
 *
 * `trip_not_found` means something different here than it does anywhere else. Edits save on a
 * debounce, so a rider who places two points and immediately presses download is asking for a
 * document that does not exist yet — which is a wait, not a loss, and telling them their trip is
 * gone would be alarming and wrong.
 */
function exportErrorMessage(error: unknown): string {
  if (isApiError(error) && error.code === 'trip_not_found') {
    return 'Nothing saved to export yet. Give it a moment and try again.'
  }
  return routeErrorMessage(error)
}

export function GpxExport({
  client,
  slug,
  tripName,
  waypointCount,
  placeCount,
  sink,
}: GpxExportProps): React.JSX.Element {
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<unknown>(null)
  const running = useRef(false)

  const download = useCallback(() => {
    // One at a time. A double-click on a slow export would fetch the same ten-thousand-point
    // track twice and save two copies of it.
    if (running.current || slug === null) return
    running.current = true
    setStatus('working')
    setError(null)

    client.exportGpx(slug, {}).then(
      (blob) => {
        running.current = false
        setStatus('idle')
        saveBlob(blob, gpxFilename(tripName ?? '', slug), sink)
      },
      (reason: unknown) => {
        running.current = false
        // 501 is a promise, not a failure. Presenting it in red teaches a rider to distrust the
        // control once it starts working.
        setStatus(isNotImplemented(reason) ? 'unavailable' : 'failed')
        setError(reason)
      },
    )
  }, [client, slug, tripName, sink])

  const routable = waypointCount >= 2

  return (
    <div className="gpx">
      <button type="button" onClick={download} disabled={!routable || slug === null}>
        Download GPX
      </button>

      <p className="gpx__contents">
        {/* What is in the file, in the rider's terms. The place count is the document's, which is
            what gets exported; the track's point count is the backend's to decide after thinning,
            so it is described rather than quoted. */}
        {`The route as a track${placeCount > 0 ? `, plus ${String(placeCount)} places as waypoints` : ''}. `}
        Long routes are thinned to fit GPS point limits.
      </p>

      {status === 'working' && (
        <p className="gpx__pending" role="status">
          Preparing the file&hellip;
        </p>
      )}

      {status === 'unavailable' && <p className="gpx__pending">Export is not built yet.</p>}

      {status === 'failed' && (
        <p className="gpx__error" role="alert">
          {exportErrorMessage(error)}
        </p>
      )}
    </div>
  )
}
