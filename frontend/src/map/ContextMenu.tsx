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
import { useEffect, useRef } from 'react'

export interface ContextMenuItem {
  readonly key: string
  readonly label: string
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

export function ContextMenu({ at, items, onDismiss }: ContextMenuProps): React.JSX.Element | null {
  const menuRef = useRef<HTMLDivElement>(null)

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
      style={{ left: `${String(at.x)}px`, top: `${String(at.y)}px` }}
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
          {item.label}
        </button>
      ))}
    </div>
  )
}
