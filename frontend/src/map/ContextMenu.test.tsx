import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ContextMenu } from './ContextMenu'

/**
 * A right-click menu on the map.
 *
 * Tim asked for one, and it fixes something nobody had flagged: right-click already *did*
 * something on both pin types — remove a waypoint, add a place to the route — with no label, no
 * confirmation and no way to discover it. An unlabelled destructive right-click is the worst of
 * both, because the rider who finds it finds it by accident.
 *
 * The menu is a list of named actions, so what each one does is legible before it happens, and
 * dismissing it costs nothing.
 */

const AT = { x: 120, y: 240 }

describe('ContextMenu', () => {
  it('names each action rather than relying on where the rider clicked', () => {
    render(
      <ContextMenu
        at={AT}
        items={[
          { key: 'remove', label: 'Remove this point', onChoose: vi.fn() },
          { key: 'split', label: 'Add point here', onChoose: vi.fn() },
        ]}
        onDismiss={vi.fn()}
      />,
    )

    expect(screen.getByRole('menuitem', { name: 'Remove this point' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Add point here' })).toBeInTheDocument()
  })

  it('does the thing and closes', () => {
    const onChoose = vi.fn()
    const onDismiss = vi.fn()
    render(
      <ContextMenu
        at={AT}
        items={[{ key: 'remove', label: 'Remove this point', onChoose }]}
        onDismiss={onDismiss}
      />,
    )

    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove this point' }))

    expect(onChoose).toHaveBeenCalled()
    expect(onDismiss).toHaveBeenCalled()
  })

  it('closes on Escape, which is the reflex', () => {
    const onDismiss = vi.fn()
    render(<ContextMenu at={AT} items={[{ key: 'a', label: 'A', onChoose: vi.fn() }]} onDismiss={onDismiss} />)

    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })

    expect(onDismiss).toHaveBeenCalled()
  })

  it('closes when the rider clicks anywhere else', () => {
    // A menu that stays up after you have decided against it is a menu you have to fight.
    const onDismiss = vi.fn()
    render(<ContextMenu at={AT} items={[{ key: 'a', label: 'A', onChoose: vi.fn() }]} onDismiss={onDismiss} />)

    fireEvent.pointerDown(document.body)

    expect(onDismiss).toHaveBeenCalled()
  })

  it('does not close when the rider clicks inside it', () => {
    const onDismiss = vi.fn()
    render(<ContextMenu at={AT} items={[{ key: 'a', label: 'A', onChoose: vi.fn() }]} onDismiss={onDismiss} />)

    fireEvent.pointerDown(screen.getByRole('menu'))

    expect(onDismiss).not.toHaveBeenCalled()
  })

  it('opens where the rider clicked', () => {
    render(<ContextMenu at={AT} items={[{ key: 'a', label: 'A', onChoose: vi.fn() }]} onDismiss={vi.fn()} />)

    const menu = screen.getByRole('menu')
    expect(menu.style.left).toBe('120px')
    expect(menu.style.top).toBe('240px')
  })

  it('takes focus so a keyboard can reach it', () => {
    // A menu opened by the pointer is still a menu, and arrow keys and Escape have to land
    // somewhere.
    render(<ContextMenu at={AT} items={[{ key: 'a', label: 'A', onChoose: vi.fn() }]} onDismiss={vi.fn()} />)

    expect(screen.getByRole('menu')).toHaveFocus()
  })

  it('marks a destructive action as one', () => {
    // Removing a point is not the same kind of thing as adding one, and a menu that presents
    // them identically invites the mistake.
    render(
      <ContextMenu
        at={AT}
        items={[{ key: 'remove', label: 'Remove this point', destructive: true, onChoose: vi.fn() }]}
        onDismiss={vi.fn()}
      />,
    )

    expect(screen.getByRole('menuitem').className).toMatch(/destructive/)
  })

  it('renders nothing at all when there is nothing to offer', () => {
    // An empty menu is a box that appears and does nothing, which reads as a bug.
    render(<ContextMenu at={AT} items={[]} onDismiss={vi.fn()} />)

    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
