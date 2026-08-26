import { fireEvent, render, screen } from '@testing-library/react'
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

/**
 * The still-alive signal when the figure *is* known.
 *
 * Judging is a single LLM call of about eighteen seconds and it cannot be subdivided — the judge
 * compares candidates against each other, so splitting the batch would damage the ranking. The
 * bar therefore sits honestly still at around half way for eighteen seconds. Backend offered a
 * heartbeat event; the better answer is on this side, because animation costs no events, needs
 * no protocol, and covers every silent stretch rather than the one stage we happen to know
 * about today.
 *
 * This is the branch's own stated principle — "the animation carries the still-alive signal on
 * its own" — applied to the case it was not written for.
 */
describe('ReplanProgress while the figure holds still', () => {
  it('marks the bar as working whenever a run is going, figure or not', () => {
    render(
      <ReplanProgress
        isRunning
        progress={0.5}
        elapsedS={20}
        log={[{ id: 1, message: 'Scoring candidates', stage: 'judge' }]}
      />,
    )

    expect(screen.getByRole('progressbar').className).toMatch(/progress__bar--working/)
  })

  it('stops marking it the moment the run ends', () => {
    // A finished bar that still shimmers claims work that is over.
    render(
      <ReplanProgress
        isRunning={false}
        progress={1}
        elapsedS={41}
        log={[{ id: 1, message: 'Done', stage: 'done' }]}
      />,
    )

    expect(screen.getByRole('progressbar').className).not.toMatch(/progress__bar--working/)
  })

  it('still reports the figure it has, so the animation adds to it rather than replacing it', () => {
    render(
      <ReplanProgress
        isRunning
        progress={0.5}
        elapsedS={20}
        log={[{ id: 1, message: 'Scoring candidates', stage: 'judge' }]}
      />,
    )

    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '50')
    expect(bar).toHaveStyle({ width: '50%' })
  })
})

/**
 * The history behind the current step, folded away.
 *
 * Tim, after planning a real trip: *"the discovery action log is too long. Maybe it could be a
 * hidden dropdown arrow that you can expand if you want to see? For most cases the white text and
 * blue progress meter are enough."*
 *
 * Read carefully, that is not a request for less information — it says the current line and the
 * meter **are** doing their job and the accumulation behind them is noise. I built the receding
 * list because an accumulating log was asked for; he has now used it and told us the accumulation
 * is the part he does not want by default. So it is collapsed rather than removed, and the count
 * is on the disclosure so it reads as available rather than absent.
 */
describe('ReplanProgress history', () => {
  const three = [
    { id: 3, message: 'Scoring 41 candidates', stage: 'judge' },
    { id: 2, message: 'Resolved 29 places', stage: 'resolve' },
    { id: 1, message: 'Searched 12 corridors', stage: 'search' },
  ]

  it('shows the current step without folding it away', () => {
    // The one line he says is enough. Behind a disclosure it would be the feature being removed.
    render(<ReplanProgress isRunning progress={0.5} elapsedS={20} log={three} />)

    expect(screen.getByText('Scoring 41 candidates')).toBeVisible()
  })

  it('folds the earlier steps behind a disclosure, closed', () => {
    render(<ReplanProgress isRunning progress={0.5} elapsedS={20} log={three} />)

    const disclosure = screen.getByRole('group')
    expect(disclosure).not.toHaveAttribute('open')
  })

  it('says how many are behind it, so it reads as available rather than absent', () => {
    render(<ReplanProgress isRunning progress={0.5} elapsedS={20} log={three} />)

    expect(screen.getByText(/2 earlier steps/i)).toBeInTheDocument()
  })

  it('counts one earlier step in the singular', () => {
    render(
      <ReplanProgress
        isRunning
        progress={0.5}
        elapsedS={20}
        log={[three[0] as (typeof three)[number], three[1] as (typeof three)[number]]}
      />,
    )

    expect(screen.getByText(/1 earlier step\b/i)).toBeInTheDocument()
  })

  it('shows them when the rider opens it', () => {
    render(<ReplanProgress isRunning progress={0.5} elapsedS={20} log={three} />)

    fireEvent.click(screen.getByText(/2 earlier steps/i))

    expect(screen.getByText('Searched 12 corridors')).toBeVisible()
  })

  it('offers no disclosure when there is no history behind the current step', () => {
    // An empty "0 earlier steps" control is a thing to click that does nothing.
    render(
      <ReplanProgress
        isRunning
        progress={0.5}
        elapsedS={20}
        log={[three[0] as (typeof three)[number]]}
      />,
    )

    expect(screen.queryByRole('group')).not.toBeInTheDocument()
  })
})
