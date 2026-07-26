/**
 * A checkbox multi-select in a popover, used for the edge-type and node-type
 * filters.
 *
 * Local to this view. If the search or review slice ever needs the same
 * control, this is the thing to hoist into `src/components/`.
 */

import { useEffect, useId, useRef, useState } from "react";

interface TypeFilterProps {
  /** Control label, e.g. "Edge types". */
  label: string;
  /** Every type id that can be picked, sorted. */
  options: readonly string[];
  /** The currently-filtered type ids; empty means "any". */
  selected: readonly string[];
  /** Called with the new selection. */
  onChange: (next: string[]) => void;
  /** Shown in the summary when nothing is selected. */
  anyLabel: string;
  /** Extra line inside the popover, e.g. the root-exemption note. */
  note?: string;
}

/**
 * Render the multi-select.
 *
 * @param props See {@link TypeFilterProps}.
 */
export function TypeFilter({ label, options, selected, onChange, anyLabel, note }: TypeFilterProps) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const listId = useId();

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const toggle = (type: string) => {
    onChange(
      selected.includes(type) ? selected.filter((it) => it !== type) : [...selected, type].sort(),
    );
  };

  const summary =
    selected.length === 0
      ? anyLabel
      : selected.length === 1
        ? selected[0]
        : `${selected.length} selected`;

  return (
    <div className="nd-graph__popover" ref={wrapperRef}>
      <span className="nd-label">{label}</span>
      <button
        type="button"
        className="nd-button nd-button--small nd-graph__popover-trigger"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="nd-truncate">{summary}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="nd-graph__popover-panel" id={listId}>
          {note ? <p className="nd-graph__popover-note">{note}</p> : null}
          {options.length === 0 ? (
            <p className="nd-graph__popover-note">No types to offer.</p>
          ) : (
            <ul className="nd-graph__checklist">
              {options.map((type) => (
                <li key={type}>
                  <label className="nd-graph__check">
                    <input
                      name={`graph-type-${type}`}
                      type="checkbox"
                      checked={selected.includes(type)}
                      onChange={() => toggle(type)}
                    />
                    <span className="nd-mono">{type}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          {selected.length > 0 ? (
            <button
              type="button"
              className="nd-button nd-button--ghost nd-button--small"
              onClick={() => onChange([])}
            >
              Clear
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
