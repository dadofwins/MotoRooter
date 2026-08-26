/**
 * A right-click menu on the map.
 *
 * Tim asked for one, and it fixes something nobody had flagged. Right-click already *did*
 * something on both pin types — removed a waypoint, added a place to the route — with no label,
 * no confirmation and no way to discover it. An unlabelled destructive right-click is the worst
 * of both: the rider who finds it finds it by accident, and usually by doing it.
 *
 * Deliberately dumb. It knows where it is, what it offers and how to close; it does not know what
 * a waypoint or a place is. The caller assembles the items, so the menu cannot drift out of step
 * with what the map can actually do.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

export interface ContextMenuItem {
  readonly key: string
  readonly label: string
  /**
   * A note beside the label — what kind of place a row is, when the menu is listing places.
   *
   * Its own element rather than folded into the label, so it can be quiet on screen — but still
   * inside the button, so it is announced too. Choosing between two campgrounds and a diner is
   * exactly the moment the kind matters.
   */
  readonly hint?: string
  /** Removing a point is not the same kind of thing as adding one. */
  readonly destructive?: boolean
  readonly onChoose: () => void
}

export interface ContextMenuProps {
  /** Where the rider clicked, in viewport pixels. */
  readonly at: { readonly x: number; readonly y: number }
  readonly items: readonly ContextMenuItem[]
  readonly onDismiss: () => void
}

/** Breathing room between the menu and the edge it was pushed off. */
const EDGE_MARGIN_PX = 8

export function ContextMenu({ at, items, onDismiss }: ContextMenuProps): React.JSX.Element | null {
  const menuRef = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState(at)

  /**
   * Pull the menu back on screen if the cursor was near an edge.
   *
   * Measured after layout rather than guessed from the item count: the height depends on the
   * font the rider's browser gives it. A menu whose last item is below the fold has an item
   * nobody can choose, and on a place pin that item is the destructive one.
   */
  useLayoutEffect(() => {
    const menu = menuRef.current
    if (menu === null) return
    const { width, height } = menu.getBoundingClientRect()
    setPosition({
      // Never past the top-left: a menu bigger than the window cannot fit either way, and the
      // first item is the half worth keeping.
      x: Math.max(EDGE_MARGIN_PX, Math.min(at.x, window.innerWidth - width - EDGE_MARGIN_PX)),
      y: Math.max(EDGE_MARGIN_PX, Math.min(at.y, window.innerHeight - height - EDGE_MARGIN_PX)),
    })
  }, [at, items])

  // Focus moves in so Escape and the arrow keys have somewhere to land. A menu opened by the
  // pointer is still a menu.
  useEffect(() => {
    menuRef.current?.focus()
  }, [])

  useEffect(() => {
    // Pointerdown rather than click, so the menu is gone before whatever was underneath reacts —
    // otherwise dismissing it also places a waypoint.
    const dismissOnOutside = (event: PointerEvent): void => {
      const menu = menuRef.current
      if (menu !== null && event.target instanceof Node && menu.contains(event.target)) return
      onDismiss()
    }
    document.addEventListener('pointerdown', dismissOnOutside)
    return () => {
      document.removeEventListener('pointerdown', dismissOnOutside)
    }
  }, [onDismiss])

  // Nothing to offer, so nothing to show. An empty box that appears and does nothing reads as a
  // bug rather than as an absence.
  if (items.length === 0) return null

  return (
    <div
      className="context-menu"
      role="menu"
      tabIndex={-1}
      ref={menuRef}
      style={{ left: `${String(position.x)}px`, top: `${String(position.y)}px` }}
      onKeyDown={(pressed) => {
        if (pressed.key === 'Escape') onDismiss()
      }}
    >
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          role="menuitem"
          className={`context-menu__item${item.destructive === true ? ' context-menu__item--destructive' : ''}`}
          onClick={() => {
            item.onChoose()
            onDismiss()
          }}
        >
          <span className="context-menu__label">{item.label}</span>
          {item.hint !== undefined && <span className="context-menu__hint">{item.hint}</span>}
        </button>
      ))}
    </div>
  )
}
