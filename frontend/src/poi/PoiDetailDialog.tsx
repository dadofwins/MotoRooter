/**
 * The place detail dialog.
 *
 * Three constraints shape it, none of them about layout.
 *
 * **Nothing is kept.** Google's terms allow storing `place_id` indefinitely and essentially
 * nothing else, so everything fetched here lives in this component's state and dies with it.
 * Reopening the dialog asks again. That is the cost of the terms, not an oversight.
 *
 * **501 is not a failure.** `GET /api/places/{place_id}` is still a stub; the client raises a
 * distinct error for it so this can say "not built yet" instead of showing a rider something
 * red about a system that is working as intended.
 *
 * **Sparse is the normal case.** Dispersed camping is what this app is for and what Places
 * knows least about, so a place with a name, a coordinate and nothing else is the expected
 * result — and gets a designed state rather than an empty shell where a rating should be.
 */
import { useEffect, useRef, useState } from 'react'
import type { ApiClient } from '../api/client'
import { isNotImplemented } from '../api/errors'
import type { Poi, PoiDetail } from '../api/types'
import { isVerified, poiLabel } from '../map/poiPin'
import { placeErrorMessage } from '../trip/routeErrorMessage'

export type PlaceReader = Pick<ApiClient, 'placeDetail'>

export interface PoiDetailDialogProps {
  readonly poi: Poi
  readonly client: PlaceReader
  readonly onClose: () => void
  /** Absent for an unconfirmed suggestion, which cannot be pinned to a route. */
  readonly onAddToRoute?: (poi: Poi) => void
  /**
   * Take this place off the trip.
   *
   * Offered even where "Add to route" is not: an unconfirmed suggestion cannot be pinned, but it
   * is exactly the clutter a rider most wants gone.
   */
  readonly onIgnore?: (poi: Poi) => void
}

/**
 * Stars for a rating, as decoration over a sentence.
 *
 * Half-stars rather than rounding: 4.5 shown as five stars overstates the place, and as four
 * understates it. The glyphs are `aria-hidden` and the same fact is written out beside them —
 * glyphs alone fail in sunlight, at a glance, and with a screen reader, and this is a number a
 * rider uses to choose where to sleep.
 */
function stars(rating: number): string {
  const whole = Math.floor(rating)
  const half = rating - whole >= 0.25 && rating - whole < 0.75
  const rounded = rating - whole >= 0.75 ? whole + 1 : whole
  return '★'.repeat(rounded) + (half ? '⯨' : '') + '☆'.repeat(Math.max(0, 5 - rounded - (half ? 1 : 0)))
}

/** Five decimals is about a metre — enough to type into a GPS and find the pull-out. */
function formatCoordinate({ lat, lon }: Poi['coordinate']): string {
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`
}

type Status = 'loading' | 'ready' | 'unavailable' | 'failed' | 'nothing-to-ask'

export function PoiDetailDialog({
  poi,
  client,
  onClose,
  onAddToRoute,
  onIgnore,
}: PoiDetailDialogProps): React.JSX.Element {
  const verified = isVerified(poi)
  const placeId = poi.place_id ?? null

  const [detail, setDetail] = useState<PoiDetail | null>(null)
  const [status, setStatus] = useState<Status>(placeId === null ? 'nothing-to-ask' : 'loading')
  const [error, setError] = useState<Error | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // A POI with no place_id has no listing to look up. Asking anyway would be a request
    // certain to fail.
    if (placeId === null) return undefined

    const controller = new AbortController()
    client.placeDetail(placeId, { signal: controller.signal }).then(
      (response) => {
        if (controller.signal.aborted) return
        setDetail(response.detail)
        setStatus('ready')
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return
        // Not built yet is a fact about the system, not a fault the rider should see in red.
        if (isNotImplemented(reason)) {
          setStatus('unavailable')
          return
        }
        setError(reason instanceof Error ? reason : new Error(String(reason)))
        setStatus('failed')
      },
    )

    return () => {
      controller.abort()
    }
  }, [client, placeId])

  // Focus moves in, so a keyboard user is where the new content is and Escape reaches it.
  useEffect(() => {
    dialogRef.current?.focus()
  }, [])

  // Which photo is large. Reset by remount: the dialog is created per place, so there is no
  // stale index to carry between them.
  const [shownPhoto, setShownPhoto] = useState(0)

  const rating = detail?.rating ?? null
  const ratingCount = detail?.user_rating_count ?? null
  const photos = detail?.photo_urls ?? []
  const hasListing =
    rating !== null ||
    photos.length > 0 ||
    (detail?.reviews.length ?? 0) > 0 ||
    (detail?.opening_hours.length ?? 0) > 0 ||
    (detail?.phone ?? null) !== null ||
    (detail?.website ?? null) !== null

  return (
    <div
      className="poi-dialog"
      role="dialog"
      aria-modal="true"
      aria-label={poi.name}
      tabIndex={-1}
      ref={dialogRef}
      onKeyDown={(event) => {
        if (event.key === 'Escape') onClose()
      }}
    >
      <header className="poi-dialog__head">
        {/* Known without asking anyone: showing it immediately makes the click feel answered. */}
        <h2 className="poi-dialog__name">{poi.name}</h2>
        <p className="poi-dialog__kind">{poiLabel(poi.category)}</p>
      </header>

      {poi.note !== null && poi.note !== undefined && <p className="poi-dialog__note">{poi.note}</p>}

      {!verified && (
        // Stated as a fact about the suggestion, not as an error, and stated rather than
        // left as a control that quietly does nothing.
        <p className="poi-dialog__warning">
          This place has not been confirmed against a real listing, so it cannot be added to
          the route yet.
        </p>
      )}

      {status === 'loading' && (
        <p role="status" className="poi-dialog__pending">
          Loading details&hellip;
        </p>
      )}

      {status === 'unavailable' && (
        <p className="poi-dialog__pending">Listing details are not available yet.</p>
      )}

      {status === 'failed' && error !== null && (
        <p role="alert" className="poi-dialog__warning">
          {placeErrorMessage(error)}
        </p>
      )}

      {status === 'ready' && detail !== null && (
        <>
          {photos.length > 0 && (
            <div className="poi-dialog__gallery">
              <img
                className="poi-dialog__photo"
                src={photos[shownPhoto] ?? photos[0]}
                // Numbered, so a screen-reader user knows there are others and which one this
                // is. Places photos come with no description to use instead.
                alt={`${poi.name}, photo ${String(shownPhoto + 1)} of ${String(photos.length)}`}
                loading="lazy"
              />
              {photos.length > 1 && (
                // Buttons rather than clickable images: this is a control, and it has to be
                // reachable by keyboard.
                <div className="poi-dialog__thumbs">
                  {photos.map((url, index) => (
                    <button
                      key={url}
                      type="button"
                      className="poi-dialog__thumb"
                      aria-label={`Show photo ${String(index + 1)} of ${String(photos.length)}`}
                      aria-current={index === shownPhoto}
                      onClick={() => setShownPhoto(index)}
                    >
                      <img src={url} alt="" loading="lazy" />
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {rating !== null && (
            <p className="poi-dialog__rating">
              <span className="poi-dialog__stars" aria-hidden="true">
                {stars(rating)}
              </span>{' '}
              <span className="poi-dialog__rating-text">
                {rating.toFixed(1)} out of 5
                {ratingCount !== null &&
                  ` · ${String(ratingCount)} ${ratingCount === 1 ? 'rating' : 'ratings'}`}
              </span>
            </p>
          )}

          {!hasListing && (
            // The expected outcome for dispersed camping, said plainly. What follows is the
            // part a rider can actually use.
            <p className="poi-dialog__pending">
              No listing details for this place — which is normal for anywhere wild.
            </p>
          )}

          {detail.opening_hours.length > 0 && (
            <ul className="poi-dialog__hours">
              {detail.opening_hours.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}

          <ul className="poi-dialog__links">
            {detail.phone !== null && detail.phone !== undefined && (
              <li>
                <a href={`tel:${detail.phone}`}>{detail.phone}</a>
              </li>
            )}
            {detail.website !== null && detail.website !== undefined && (
              <li>
                {/* noreferrer as well as noopener: an outbound link should not leak where
                    the rider came from. */}
                <a href={detail.website} target="_blank" rel="noopener noreferrer">
                  Website
                </a>
              </li>
            )}
          </ul>

          {detail.reviews.length > 0 && (
            // Fetched on every call and never rendered until now. What a place is actually like
            // is the thing a rating cannot tell you.
            <ul className="poi-dialog__reviews">
              {detail.reviews.map((review) => (
                <li key={review}>{review}</li>
              ))}
            </ul>
          )}
        </>
      )}

      <p className="poi-dialog__coordinate">{formatCoordinate(poi.coordinate)}</p>

      <div className="poi-dialog__actions">
        {verified && onAddToRoute !== undefined && (
          // The same action as right-clicking the pin, made discoverable: nobody finds a
          // right-click menu they were not told about.
          <button type="button" onClick={() => onAddToRoute(poi)}>
            Add to route
          </button>
        )}
        {onIgnore !== undefined && (
          // The other half of choosing, and offered whether or not the place can be routed
          // through. Durable: discovered places are saved into the trip document, so hiding one
          // without removing it would bring it straight back on the next load.
          <button type="button" onClick={() => onIgnore(poi)}>
            Ignore
          </button>
        )}
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  )
}
