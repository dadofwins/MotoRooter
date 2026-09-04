/**
 * What counts as a different trip, for the purpose of describing it.
 *
 * The rail header's line comes from a model call, so regenerating it on every change would
 * spend an LLM request per drag commit — the quota failure the drag throttle exists to
 * prevent, arriving through a different door.
 *
 * **The guarantee that matters is structural and is not here.** This key is computed from the
 * *committed* trip content — the same triple `useTripSave` persists. `App` holds mid-drag
 * geometry in a separate `preview` state (`shownLegs = preview ?? legs`) and only `onCommit`
 * writes into the edit, so preview geometry has no path in at all, whatever this function is
 * made of. That is a property rather than a threshold; tuning a key not to fire too often
 * would be a knob. This function is the second line: given two committed contents, is the trip
 * meaningfully different?
 *
 * **Not the coordinate.** `Waypoint` carries no id, so "the same waypoint" has to be
 * recognised by something, and the coordinate is exactly the field that moves while nothing
 * about the trip changes — nudging a point along the road it is already on is not a new ride.
 * What a line about a trip would actually draw on is where it goes, how it is ridden, what is
 * on it, and roughly how far: names in order, the modes, the place categories, a coarse
 * distance.
 *
 * **The gap this accepts, deliberately.** `Waypoint.name` is nullable, so an unnamed point can
 * be dragged a long way with no name to change. `DISTANCE_BUCKET_M` catches that once the move
 * is large; below it the line goes briefly stale. That is the right way round — the header is
 * decoration, and a rider loses nothing they can act on, whereas a model call per drag commit
 * costs quota on the one path that must never pay. The bucket is the lever if it ever needs to
 * be fresher.
 */
import type { Poi, TripLeg, Waypoint } from '../api/types'

/**
 * The committed trip content, which is exactly what `useTripSave` persists.
 *
 * Structural rather than `Trip` on purpose. The document a session *built* is never read back
 * — `useStoredTrip` only populates on a URL load or an explicit reload — so a `Trip` would be
 * null for the app's main flow. This is the same triple the save writes, so keying on it means
 * keying on what has been committed, which is the property that was wanted all along.
 */
export interface BlurbInput {
  readonly waypoints: readonly Waypoint[]
  readonly legs: readonly TripLeg[]
  readonly pois: readonly Poi[]
}

/**
 * How coarsely distance is compared.
 *
 * Wide enough that re-routing the same trip does not cross it — engine disagreement and a
 * nudged waypoint move a route by metres to hundreds of metres — and narrow enough that a
 * waypoint dragged to somewhere genuinely else does.
 */
const DISTANCE_BUCKET_M = 10_000

/**
 * How many of something, as coarsely as a rider would say it.
 *
 * The boundaries are the ones ordinary speech uses, which is the same vocabulary the blurb
 * itself reaches for. Anything finer would distinguish counts nobody describes differently.
 */
function howMany(count: number): string {
  if (count === 1) return 'one'
  if (count <= 4) return 'a few'
  return 'many'
}

export function tripBlurbKey(trip: BlurbInput): string {
  // In order: a route ridden the other way round is a different ride, and a set would call
  // the two the same.
  const places = trip.waypoints.map((waypoint) => waypoint.name ?? '').join('␟')

  // Per-leg, not `default_intent`. Mode is a property of each segment, so a trip whose
  // default never moves can still turn from dirt into tarmac one leg at a time.
  const modes = trip.legs.map((leg) => leg.intent).join(',')

  // How many of each kind, coarsely. Three properties have to hold at once, and only a
  // bucket per category has all three:
  //
  //   - five campgrounds swapped for five hotels is a different ride, so a bare count is out;
  //   - one campground becoming several is a different ride too — a live blurb read "a few
  //     camps" off three places, so the count genuinely reaches the prose and a bare set of
  //     categories would leave "a camp" standing over five;
  //   - five camps and six camps is *not* a different ride, and this is the one that bites:
  //     `ReplanEvent.pois` is cumulative per stage and `useReplan` takes places incrementally,
  //     so one entry per POI is one model call per event the day discovery streams them. That
  //     is a header rewriting itself through a two-minute replan.
  //
  // Buckets are the model's own vocabulary rather than a scale invented here — it says "a few
  // camps", not "three camps" — so they cost no fidelity the line was using.
  const counted = new Map<string, number>()
  for (const poi of trip.pois) counted.set(poi.category, (counted.get(poi.category) ?? 0) + 1)
  const kinds = [...counted]
    // Sorted, because discovery promises no order and a re-run returning the same places
    // shuffled is not a different trip.
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([category, count]) => `${category}:${howMany(count)}`)
    .join(',')

  // From the legs rather than `total_distance_m`, which the coverage allowlist records as
  // deliberately unread — the rail recomputes from legs so its figure stays live during an
  // edit. An unrouted leg contributes nothing, which is correct: it has no distance yet.
  const distanceM = trip.legs.reduce((total, leg) => total + (leg.routed?.distance_m ?? 0), 0)
  const bucket = Math.round(distanceM / DISTANCE_BUCKET_M)

  return [trip.waypoints.length, places, modes, kinds, bucket].join('␞')
}
