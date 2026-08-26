/**
 * What a discovery run looks for.
 *
 * The last mouse-equivalence gap. `find_places` takes categories; the mouse could only say
 * "everything", so "find me more restaurants" worked by typing and not by clicking.
 *
 * Per category rather than per group, deliberately. Groups would have been fewer controls and a
 * neater rail, and they would have left the assistant able to ask for something the mouse still
 * could not — a narrower gap is still a gap. The groups organise nine chips for the eye.
 *
 * It sits beside the button it changes rather than in a settings dialog, because it is part of
 * *this run* rather than a standing preference, and because it is a cost control: discovery fans
 * out one metered search per anchor per category. That last part is stated on screen, since a
 * rider who does not know it has no reason to narrow anything.
 */
import { ALL_CATEGORIES, CATEGORY_GROUPS } from './discoveryCategories'
import { poiLabel } from '../map/poiPin'
import type { PoiCategory } from '../api/types'

export interface CategoryPickerProps {
  readonly selected: readonly PoiCategory[]
  readonly onChange: (next: readonly PoiCategory[]) => void
  /** True while a run is going: it has already been priced, so the choice is fixed. */
  readonly disabled: boolean
}

export function CategoryPicker({
  selected,
  onChange,
  disabled,
}: CategoryPickerProps): React.JSX.Element {
  const chosen = new Set(selected)

  const toggle = (category: PoiCategory): void => {
    const next = new Set(chosen)
    if (next.has(category)) {
      // A run with no categories finds nothing and still pays for the route-search stage.
      // Refusing the last untick is kinder than offering a button that cannot work.
      if (next.size === 1) return
      next.delete(category)
    } else {
      next.add(category)
    }
    // Offered order, not click order, so the request, the picker and the list of results all
    // read the same way round.
    onChange(ALL_CATEGORIES.filter((each) => next.has(each)))
  }

  return (
    <div className="categories">
      {CATEGORY_GROUPS.map(({ group, title, categories }) => (
        <div key={group} className="categories__group">
          <span className="categories__group-title">{title}</span>
          <div className="categories__chips">
            {categories.map((category) => (
              <label
                key={category}
                className={`categories__chip${chosen.has(category) ? ' categories__chip--on' : ''}`}
              >
                <input
                  type="checkbox"
                  checked={chosen.has(category)}
                  disabled={disabled}
                  onChange={() => toggle(category)}
                />
                {poiLabel(category)}
              </label>
            ))}
          </div>
        </div>
      ))}

      <p className="categories__cost">
        {`${String(chosen.size)} of ${String(ALL_CATEGORIES.length)} kinds`}
        {chosen.size < ALL_CATEGORIES.length && ' · fewer searches, so a faster and cheaper run'}
      </p>
    </div>
  )
}
