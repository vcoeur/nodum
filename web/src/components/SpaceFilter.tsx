/**
 * The shared read-side space filter (design decision D1).
 *
 * A space reaches the human as **two independent controls**, and this is the
 * read one: it *narrows* and never widens, and it defaults to `any`, so a view
 * that has not been narrowed still spans the whole file. The write target is
 * the other control and lives in `lib/writeTarget.ts` — reading `research`
 * while still filing into `main` is the ordinary case, which is why one
 * switcher could not have served both.
 *
 * Presentational and controlled on purpose. Search puts its value in the URL,
 * the graph keeps it in toolbar state, a listing may hold it in a hook — the
 * views own that, and a component that owned it instead would make each of them
 * fight it. This renders the value it is given and reports changes.
 */

import { spaceOptions, resolveSpaceValue } from "./spaceOptions";
import type { NodeOut } from "../api/types";

export { ANY_SPACE } from "./spaceOptions";

interface SpaceFilterProps {
  /** The selected space id or name; `""` (`ANY_SPACE`) for no filter. */
  value: string;
  /** Called with the new value — a space id, or `""` for no filter. */
  onChange: (value: string) => void;
  /** Active spaces from `GET /api/spaces`; null while loading or after a failure. */
  spaces: readonly NodeOut[] | null;
  /** True once the space list request failed — the control says so rather than
   *  presenting "Any space" as a choice the human made. */
  failed?: boolean;
  /** Field label. Defaults to "Space". */
  label?: string;
  /** Extra class on the wrapping label, for the view's own layout. */
  className?: string;
  /**
   * Extra class on the `<select>` itself, for a filter row that sizes its own
   * controls.
   *
   * The alternative — a view-side `.my-row .nd-select {…}` override — reaches
   * inside a shared component from outside it, which is how one view's layout
   * silently starts styling another's. A named prop keeps the seam where the
   * component can see it.
   */
  controlClassName?: string;
}

/**
 * Render the space filter.
 *
 * @param value Current selection (id or name); `""` for no filter.
 * @param onChange Applies the new selection.
 * @param spaces The live space list, or null while it is unknown.
 * @param failed Whether loading the space list failed.
 * @param label Field label.
 * @param className Extra class for the view's layout.
 * @param controlClassName Extra class for the `<select>`, for a filter row that
 *   sizes its controls.
 */
export function SpaceFilter({
  value,
  onChange,
  spaces,
  failed = false,
  label = "Space",
  className,
  controlClassName,
}: SpaceFilterProps) {
  const known = spaces ?? [];
  const options = spaceOptions(known, value, failed ? "Spaces unavailable" : "Any space");
  // The list may not be loaded yet while the value already names a space (a
  // shared URL, a restored filter). `spaceOptions` has kept that value
  // representable; this is the matching half — the `<select>` must be given the
  // same spelling its options carry, or it renders blank and the next change
  // event overwrites a filter the human never touched.
  const selected = resolveSpaceValue(known, value);

  return (
    <label className={className ? `nd-field ${className}` : "nd-field"}>
      <span className="nd-label">{label}</span>
      <select
        className={controlClassName ? `nd-select ${controlClassName}` : "nd-select"}
        value={selected}
        disabled={spaces === null && !failed}
        title={
          failed
            ? "The space list could not be loaded — is the server running?"
            : "Narrow this view to one space. It never widens what you can see."
        }
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value || "any"} value={option.value}>
            {option.unlisted ? `${option.label} (unavailable)` : option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
