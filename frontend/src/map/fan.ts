/**
 * Opening a small group of overlapping places out into its members.
 *
 * The measurement said a list was the only thing that worked at twelve members; Tim's call was
 * to fan below that and list above it, which is the better answer where it fits — a fan keeps
 * the places on the map instead of moving them into a panel.
 *
 * A fanned pin is **not where its place is**. That is the honest cost of the idea, and what
 * keeps it from being a lie is the leader line drawn back to the group: the offset is disclosed
 * rather than hidden. The two constants below are the whole trade — far enough apart to be
 * separate pins, close enough that the group is still recognisably where the places are.
 */
import { coordinateAt, pixelsAt } from './cluster'
import type { Coordinate } from '../api/types'

/**
 * The largest group that opens as a fan. Anything bigger opens as a list.
 *
 * **Tim's number, set against a measurement.** A live corridor put twelve places in one group at
 * the zoom a rider plans at, and twelve pins on a 40px radius sit 21px apart — narrower than a
 * pin, so the fan would recreate the overlap it exists to solve. Eight is where he drew the line
 * rather than dropping the fan.
 *
 * Moving it is a one-line change and the geometry follows: the radius is derived from the count,
 * so a larger value spreads the fan wider rather than crowding it. What it costs is honesty about
 * position — see `MAX_RADIUS_PX`, which is where a fan that has grown too wide stops being one.
 */
export const FAN_MAX_MEMBERS = 8

/**
 * How far apart two fanned pins sit, centre to centre.
 *
 * A pin is 24px across with a 2px border, so 32 leaves four pixels of daylight. Below about 28
 * they overlap and the fan has achieved nothing.
 */
export const FAN_SPACING_PX = 32

/**
 * The smallest fan, so a pair is still obviously two things around a middle rather than two pins
 * that happen to be near each other.
 */
const MIN_RADIUS_PX = 44

/**
 * A little more room than the geometry strictly demands.
 *
 * Two pins exactly `FAN_SPACING_PX` apart are touching, not separated, and the projection is not
 * perfectly linear across the height of a fan — so the pair at the top and the pair at the bottom
 * are not quite the same distance apart on screen. Five per cent absorbs both.
 */
const SPACING_MARGIN = 1.05

/**
 * How far a fanned pin may be from the place it stands for.
 *
 * At zoom 12 this is roughly a kilometre and a half, which is already generous. A fan wider than
 * this is not disclosing an offset any more, it is drawing the place somewhere else — which is
 * why the member ceiling exists rather than the radius simply growing forever.
 */
const MAX_RADIUS_PX = 72

/**
 * Where to draw each member of an opened group.
 *
 * Evenly spaced around the group, first one straight up. Even rather than in the direction each
 * place actually lies: members of a group are metres apart by definition, so their true bearings
 * are noise, and a fan that reproduced them would be unreadable while looking meaningful.
 *
 * Deterministic, because a fan that reshuffles while it is open is worse than one that overlaps.
 *
 * `progress` is how far open it is, and the animation is nothing more than this same layout at a
 * growing radius. One formula rather than two, so no frame mid-flight can disagree with where the
 * pins finish — and at 0 every pin sits on the group, which is what makes them read as coming
 * *from* it rather than sliding in from nowhere.
 */
export function fanPositions(
  centre: Coordinate,
  count: number,
  zoom: number,
  progress = 1,
): readonly Coordinate[] {
  if (count <= 0) return []

  // Sized by the count, so raising the member ceiling spreads the fan rather than crowding it.
  //
  // From the **chord**, not the arc. `count * spacing / 2π` is the radius whose *circumference*
  // gives that spacing, and neighbouring pins are separated by the straight line between them,
  // which is shorter — at twelve members the arc formula delivers 31.6px where it promised 32,
  // which is how a mutation test caught this: fixing the radius at the minimum passed every test,
  // because at eight members the scaling never binds at all.
  const needed = (SPACING_MARGIN * FAN_SPACING_PX) / (2 * Math.sin(Math.PI / count))
  // Bounded at both ends: below, so a pair still reads as a fan; above, so no pin wanders off its
  // own place. The upper bound is what the member ceiling is really protecting — past about
  // thirteen members the clamp binds and the pins start to touch again whatever the radius says.
  const open = Math.min(MAX_RADIUS_PX, Math.max(MIN_RADIUS_PX, needed))
  const radius = open * Math.min(1, Math.max(0, progress))

  const hub = pixelsAt(centre, zoom)
  return Array.from({ length: count }, (_, index) => {
    // Clockwise from straight up, which is where a reader looks first and which keeps a group of
    // two vertical rather than at an arbitrary diagonal.
    const angle = (index / count) * 2 * Math.PI - Math.PI / 2
    return coordinateAt(
      { x: hub.x + radius * Math.cos(angle), y: hub.y + radius * Math.sin(angle) },
      zoom,
    )
  })
}
