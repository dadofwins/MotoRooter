/**
 * A place's map pin, small enough to sit in a line of text.
 *
 * Tim asked for the group list to carry "the icons of each one", and a bare glyph is not that:
 * the pin is a coloured shape with a white glyph in it, and matching only the character leaves
 * the rider still translating prose into shapes. This is the same three axes the pin uses —
 * shape per group, glyph per category, colour last — at a size that fits a row.
 *
 * Decoration, always. Every row that carries one also names the place and its category in words,
 * so announcing the shape as well would be noise.
 */
import { poiGlyph, poiGroup } from '../map/poiPin'
import type { PoiCategory } from '../api/types'

export function PoiMark({ category }: { readonly category: PoiCategory }): React.JSX.Element {
  return (
    <span className={`poi poi--${poiGroup(category)} poi--mark`} aria-hidden="true">
      {/* Its own element so the sight pin's counter-rotation has something to apply to. */}
      <span>{poiGlyph(category)}</span>
    </span>
  )
}
