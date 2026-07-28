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
 *
 * **A filter set to a space that has since been archived is named, and stays
 * unpickable.** The two lists arrive separately for that reason: `spaces` is the
 * vocabulary — every option a human may choose — and `archivedSpaces` names the
 * one value already selected, which the vocabulary deliberately does not carry.
 * Only the current selection is ever resolved against the second list, so
 * choosing anything else drops the retired space out of the control for good.
 */

import { spaceOptions, resolveSpaceValue, unlistedMark } from "./spaceOptions";
import { nameSpace } from "./spaceNaming";
import type { NodeOut } from "../api/types";

export { ANY_SPACE } from "./spaceOptions";

interface SpaceFilterProps {
  /** The selected space id or name; `""` (`ANY_SPACE`) for no filter. */
  value: string;
  /** Called with the new value — a space id, or `""` for no filter. */
  onChange: (value: string) => void;
  /** Active spaces from `GET /api/spaces`; null while loading or after a failure. */
  spaces: readonly NodeOut[] | null;
  /**
   * Archived space nodes, for naming a filter left pointing at one.
   *
   * Never a source of options — see the module docblock. Empty (the default) is
   * the ordinary case: the lazy read behind it fires only when the screen holds
   * a space `GET /api/spaces` cannot name.
   */
  archivedSpaces?: readonly NodeOut[];
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
  /**
   * `name` on the `<select>`. Defaults to `space`.
   *
   * Every form control carries one: a field with neither `id` nor `name` is
   * something a browser cannot address — it is what DevTools flags, and what
   * autofill, a password manager, and any assistive tooling walking the form
   * have to fall back to guessing about. There is no form here to submit, so
   * the value is never sent anywhere; the name exists to make the control a
   * named thing rather than an anonymous one.
   */
  name?: string;
  /**
   * What the control says it does, for the `title` tooltip.
   *
   * The default is the read filter's promise — *it narrows, and never widens
   * what you can see* — which is true of every view that filters a listing with
   * it. It is **not** true of the one surface that reuses the control to choose
   * what a write will act on: the journal's run-now scope decides where the
   * gardener writes, so a tooltip inherited from a read filter would tell the
   * human the opposite of what the button underneath it is about to do. A
   * caller with a different job says so here rather than the component guessing
   * from its label.
   *
   * A failed space list overrides it either way — that sentence is about the
   * control being unusable, which outranks whatever it would otherwise do.
   */
  title?: string;
}

/**
 * Render the space filter.
 *
 * @param value Current selection (id or name); `""` for no filter.
 * @param onChange Applies the new selection.
 * @param spaces The live space list, or null while it is unknown.
 * @param archivedSpaces Archived space nodes, for naming the current selection
 *   when it is one of them. Never offered as a choice.
 * @param failed Whether loading the space list failed.
 * @param label Field label.
 * @param className Extra class for the view's layout.
 * @param controlClassName Extra class for the `<select>`, for a filter row that
 *   sizes its controls.
 * @param name `name` on the `<select>`; defaults to `space`.
 * @param title What the control does, for the tooltip; defaults to the read
 *   filter's promise. A surface where the choice drives a *write* says so.
 */
export function SpaceFilter({
  value,
  onChange,
  spaces,
  archivedSpaces = [],
  failed = false,
  label = "Space",
  className,
  controlClassName,
  name = "space",
  title = "Narrow this view to one space. It never widens what you can see.",
}: SpaceFilterProps) {
  const known = spaces ?? [];
  // Resolved for the *selection only*, and handed to `spaceOptions` as a name
  // rather than as a list — that module never sees an archived space, so it
  // cannot offer one.
  const selectedName = nameSpace(value, spaces, archivedSpaces);
  const options = spaceOptions(
    known,
    value,
    selectedName,
    failed ? "Spaces unavailable" : "Any space",
  );
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
        name={name}
        className={controlClassName ? `nd-select ${controlClassName}` : "nd-select"}
        value={selected}
        disabled={spaces === null && !failed}
        title={failed ? "The space list could not be loaded — is the server running?" : title}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value || "any"} value={option.value}>
            {option.unlisted ? `${option.label} ${unlistedMark(option.archived)}` : option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
