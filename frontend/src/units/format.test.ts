import { describe, expect, it } from 'vitest'
import { formatDistance, formatDuration } from './format'

/**
 * How distances and times are written for a rider.
 *
 * One formatter, used everywhere a distance appears. Two formatters that agree today drift
 * apart within a week, and a route summary reading 305 km beside a surface breakdown adding
 * up to 189 mi is worse than either alone.
 *
 * Precision is chosen per unit rather than inherited. A rider thinks in tenths under about
 * ten of whatever unit they use and in whole numbers above it, and that threshold belongs in
 * the displayed unit — not in metres, where it would land in a different place for each.
 */

describe('formatDistance', () => {
  it('writes miles by default preference', () => {
    // 100 km is about 62 miles.
    expect(formatDistance(100_000, 'mi')).toBe('62 mi')
  })

  it('writes kilometres when that is the preference', () => {
    expect(formatDistance(100_000, 'km')).toBe('100 km')
  })

  it('keeps a tenth under ten of whichever unit is in use', () => {
    // The threshold applies to what is shown, so it lands in the same place for a reader
    // regardless of unit. 8 km is 8.0 km and 5.0 mi; both are under ten of their own unit.
    expect(formatDistance(8000, 'km')).toBe('8.0 km')
    expect(formatDistance(8000, 'mi')).toBe('5.0 mi')
  })

  it('drops the tenth above ten, where it is noise', () => {
    expect(formatDistance(20_000, 'km')).toBe('20 km')
    expect(formatDistance(20_000, 'mi')).toBe('12 mi')
  })

  it('handles the boundary consistently in both units', () => {
    // Exactly ten of the unit shown reads as whole, not as 10.0.
    expect(formatDistance(10_000, 'km')).toBe('10 km')
    expect(formatDistance(16_093.44, 'mi')).toBe('10 mi')
  })

  it('says nothing clever about zero', () => {
    expect(formatDistance(0, 'mi')).toBe('0.0 mi')
  })
})

describe('formatDuration', () => {
  /**
   * The estimate comes from a speed table the backend describes as reasoned guesses rather
   * than measurements — its own docstring notes nobody maintains 80 km/h for six hours. So
   * the wording and the rounding both hedge on purpose: "4:19" claims a confidence this
   * number has not earned.
   */
  it('reads as an approximation, because that is what it is', () => {
    expect(formatDuration(4 * 3600 + 1140)).toBe('about 4h 20m')
  })

  it('rounds to five minutes, so it cannot imply minute-level accuracy', () => {
    expect(formatDuration(4 * 3600 + 1139)).toBe('about 4h 20m') // 4h18m59s
    expect(formatDuration(4 * 3600 + 60)).toBe('about 4h')
  })

  it('drops the hours when there are none', () => {
    expect(formatDuration(45 * 60)).toBe('about 45m')
  })

  it('drops a zero minute count rather than writing 4h 0m', () => {
    expect(formatDuration(4 * 3600)).toBe('about 4h')
  })

  it('refuses to put a number on something too short to estimate', () => {
    // Two minutes of riding is inside the noise of any speed table.
    expect(formatDuration(120)).toBe('under 5m')
    expect(formatDuration(0)).toBe('under 5m')
  })

  it('carries long days without turning into days', () => {
    // A rider planning a 14-hour push wants hours, not "0.6 days".
    expect(formatDuration(14 * 3600 + 30 * 60)).toBe('about 14h 30m')
  })
})
