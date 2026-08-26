/**
 * The fan opening, rather than appearing.
 *
 * Tim asked for this in his first message about clustering — *"when you hover they fan out in a
 * circle (with animation)"* — and it is the half that says what just happened. Without it, eight
 * pins replace one and the rider has to infer that they came from somewhere.
 *
 * **No React animation library can drive it.** `framer-motion` and `react-spring` animate
 * React-rendered DOM; these are Google Maps overlays positioned imperatively, so nothing React
 * owns is moving. A CSS transition on an advanced marker's content would move the pins and not
 * the leader lines, which are map-rendered strokes — and pins that travel while their lines snap
 * looks worse than no animation. One loop drives both, which also means it works on the plain
 * marker path where there is no DOM content to transition at all.
 */

/**
 * How long the fan takes to open.
 *
 * Reasoned from what the gesture is for rather than picked. A rider clicks a group **in order to
 * click something inside it**, so the animation is in the way by definition, and Tim's two
 * standing complaints about this app have both been about waiting.
 *
 * Bounded below by what reads as motion: under about 100 ms the eye takes it as a jump, and at
 * 180 ms this is roughly eleven frames — enough to see the pins leave the group. Bounded above by
 * the app's own vocabulary: the progress bar transitions in 300 ms, and that is a bar filling
 * rather than something you are reaching for. Two thirds of it is right for something under the
 * cursor.
 *
 * The travel is 44–65 px, so this is about 300 px/s: a flick rather than a glide.
 *
 * **Nothing waits for it.** The pins exist and are clickable from the first frame, so a rider
 * quicker than the animation hits a moving target rather than a locked one.
 */
export const FAN_DURATION_MS = 180

/** Whether the rider has asked for less movement. Absence of an answer is not an answer. */
export function prefersReducedMotion(): boolean {
  const query = globalThis.matchMedia as ((query: string) => { matches: boolean }) | undefined
  if (typeof query !== 'function') return false
  return query('(prefers-reduced-motion: reduce)').matches
}

/** Fast away from the group, settling into place. A linear fan reads as mechanical. */
function easeOut(t: number): number {
  return 1 - (1 - t) ** 3
}

/**
 * Drive a fan open, reporting how far through it is.
 *
 * Returns a cancel. Cancelling stops where it is rather than snapping to the end: it is called
 * when the fan closed or the map redrew, and a flash of the fully-open fan on the way out shows
 * the rider the thing they just dismissed.
 */
export function runFanAnimation(
  durationMs: number,
  onProgress: (progress: number) => void,
): () => void {
  // The fan is the content and the motion is the decoration; only the decoration is optional.
  if (prefersReducedMotion() || durationMs <= 0) {
    onProgress(1)
    return () => undefined
  }

  let frame: number | null = null
  let startedAt: number | null = null

  const step = (timestamp: number): void => {
    startedAt ??= timestamp
    const elapsed = timestamp - startedAt
    const linear = Math.min(1, elapsed / durationMs)
    onProgress(linear >= 1 ? 1 : easeOut(linear))
    if (linear >= 1) {
      frame = null
      return
    }
    frame = requestAnimationFrame(step)
  }

  frame = requestAnimationFrame(step)
  return () => {
    if (frame !== null) cancelAnimationFrame(frame)
    frame = null
  }
}
