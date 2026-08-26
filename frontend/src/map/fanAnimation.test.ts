import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FAN_DURATION_MS, prefersReducedMotion, runFanAnimation } from './fanAnimation'

/**
 * The fan opening rather than appearing.
 *
 * Tim asked for the animation in his first message about clustering — *"when you hover they fan
 * out in a circle (with animation)"* — and it is the half that says what just happened. Without
 * it, eight pins replace one and the rider has to work out that they came from somewhere.
 *
 * **No React animation library can drive this.** `framer-motion` and `react-spring` animate
 * React-rendered DOM, and these are Google Maps overlays positioned imperatively: nothing React
 * owns is moving. A CSS transition on the marker's content would move the pins but not the leader
 * lines, which are map-rendered strokes — and pins that travel while their lines snap looks worse
 * than no animation at all. So one loop drives both, in the only place that can see both.
 */

beforeEach(() => {
  // `requestAnimationFrame` is not in vitest's default set of faked clocks, so without naming it
  // here the loop runs on real frames — which is both a real wait and flaky.
  vi.useFakeTimers({
    toFake: ['requestAnimationFrame', 'cancelAnimationFrame', 'performance', 'Date'],
  })
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('runFanAnimation', () => {
  it('starts at the group and ends at the fan', () => {
    // The two ends are what make it read as *coming from* the group rather than replacing it.
    const seen: number[] = []

    runFanAnimation(FAN_DURATION_MS, (progress) => seen.push(progress))
    vi.advanceTimersByTime(FAN_DURATION_MS * 2)

    expect(seen[0]).toBe(0)
    expect(seen.at(-1)).toBe(1)
  })

  it('only ever moves outwards', () => {
    // A pin that overshoots and comes back is a bounce, which is a different and noisier idea
    // than the one Tim asked for.
    const seen: number[] = []

    runFanAnimation(FAN_DURATION_MS, (progress) => seen.push(progress))
    vi.advanceTimersByTime(FAN_DURATION_MS * 2)

    for (let index = 1; index < seen.length; index += 1) {
      expect(seen[index]).toBeGreaterThanOrEqual(seen[index - 1] ?? 0)
    }
  })

  it('eases out, so it leaves quickly and settles', () => {
    // Most of the travel early. A linear fan reads as mechanical, and the part a rider is
    // waiting for is the end.
    const seen: number[] = []
    runFanAnimation(FAN_DURATION_MS, (progress) => seen.push(progress))

    vi.advanceTimersByTime(FAN_DURATION_MS / 2)

    expect(seen.at(-1)).toBeGreaterThan(0.5)
  })

  it('gives enough frames to read as motion rather than as two states', () => {
    const seen: number[] = []

    runFanAnimation(FAN_DURATION_MS, (progress) => seen.push(progress))
    vi.advanceTimersByTime(FAN_DURATION_MS * 2)

    expect(seen.length).toBeGreaterThanOrEqual(6)
  })

  it('stops when it is cancelled, and does not jump to the end', () => {
    // Cancelling means the fan closed or the map redrew. Snapping the pins to their full extent
    // on the way out would be a flash of the thing that was just dismissed.
    const seen: number[] = []
    const cancel = runFanAnimation(FAN_DURATION_MS, (progress) => seen.push(progress))

    vi.advanceTimersByTime(FAN_DURATION_MS / 4)
    const atCancel = seen.length
    cancel()
    vi.advanceTimersByTime(FAN_DURATION_MS * 2)

    expect(seen).toHaveLength(atCancel)
    expect(seen.at(-1)).toBeLessThan(1)
  })

  it('places the fan at once where motion is unwelcome', () => {
    // Same rule the progress meter already follows. Not "no animation and no fan" — the fan is
    // the content, the motion is the decoration, and only the decoration is optional.
    vi.stubGlobal('matchMedia', () => ({ matches: true }))
    const seen: number[] = []

    runFanAnimation(FAN_DURATION_MS, (progress) => seen.push(progress))
    vi.advanceTimersByTime(FAN_DURATION_MS * 2)

    expect(seen).toEqual([1])
  })

  it('animates where the browser will not say, rather than refusing to', () => {
    // No `matchMedia` at all — jsdom, an old browser, an embedded view. Absence of a preference
    // is not a preference against.
    vi.stubGlobal('matchMedia', undefined)

    expect(prefersReducedMotion()).toBe(false)
  })
})
