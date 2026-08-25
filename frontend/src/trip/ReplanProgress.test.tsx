import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ReplanProgress } from './ReplanProgress'
import type { ReplanStep } from './useReplan'

/**
 * What a rider sees during a run that takes minutes.
 *
 * The complaint this answers was "I have no idea what it's doing and it's already been like a
 * few minutes". So: what it is doing now, prominently; what it has done, receding; how far
 * along, without ever going backwards; how long it has been; and movement even when no event
 * has arrived for a while, because sparse events must still read as alive rather than stuck.
 */

function steps(...messages: readonly string[]): readonly ReplanStep[] {
  return messages.map((message, index) => ({ id: index, message, stage: 'discovery' }))
}

describe('ReplanProgress', () => {
  it('shows nothing at all when nothing is happening', () => {
    const { container } = render(
      <ReplanProgress isRunning={false} log={[]} progress={null} elapsedS={0} />,
    )

    expect(container).toBeEmptyDOMElement()
  })

  it('puts the current action first and lets the rest recede', () => {
    render(
      <ReplanProgress
        isRunning
        log={steps('Judging 3 of 7', 'Found 5 leads', 'Searching near Rainy Pass')}
        progress={0.42}
        elapsedS={95}
      />,
    )

    // The newest is the heading, not one row among many.
    expect(screen.getByRole('status')).toHaveTextContent('Judging 3 of 7')
    const earlier = screen.getAllByRole('listitem').map((row) => row.textContent ?? '')
    expect(earlier).toEqual(['Found 5 leads', 'Searching near Rainy Pass'])
  })

  it('reports how far along, for anyone who cannot see the bar', () => {
    render(<ReplanProgress isRunning log={steps('Judging')} progress={0.42} elapsedS={10} />)

    const meter = screen.getByRole('progressbar')
    expect(meter).toHaveAttribute('aria-valuenow', '42')
  })

  it('says how long it has been, which is the actual complaint', () => {
    render(<ReplanProgress isRunning log={steps('Judging')} progress={0.2} elapsedS={95} />)

    // Minutes and seconds: "95s" makes a rider do arithmetic about their own wait.
    expect(screen.getByText(/1m 35s/)).toBeInTheDocument()
  })

  it('counts in seconds under a minute', () => {
    render(<ReplanProgress isRunning log={steps('Judging')} progress={null} elapsedS={12} />)

    expect(screen.getByText(/12s/)).toBeInTheDocument()
  })

  it('still looks alive when no progress figure has arrived', () => {
    // Sparse events are expected, and then the animation is the only signal. An
    // indeterminate meter says "working" where a 0% bar says "stuck".
    render(<ReplanProgress isRunning log={steps('Searching')} progress={null} elapsedS={4} />)

    const meter = screen.getByRole('progressbar')
    expect(meter).not.toHaveAttribute('aria-valuenow')
    expect(meter.className).toContain('indeterminate')
  })

  it('keeps the last thing said after the run ends, so the screen does not blank', () => {
    render(
      <ReplanProgress isRunning={false} log={steps('Finished', 'Judging')} progress={1} elapsedS={130} />,
    )

    expect(screen.getByText('Finished')).toBeInTheDocument()
    // No live region once nothing is happening: there is nothing left to announce.
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
