/** The safe, app-wide command palette mounted by the authenticated shell. */

import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { clampPaletteIndex, nextPaletteIndex, paletteItems } from "../lib/commandPalette";
import type { PaletteItem } from "../lib/commandPalette";
import { useRecentNodes } from "../lib/recents";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import "./commandPalette.css";

/** The command palette's close control. */
export interface CommandPaletteProps {
  onClose(): void;
}

/** A focus-owning dialog for safe navigation, lookup, and dry-run rehearsal. */
export function CommandPalette({ onClose }: CommandPaletteProps) {
  const navigate = useNavigate();
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const optionRefs = useRef<(HTMLLIElement | null)[]>([]);
  const lookupGeneration = useRef(0);
  const listId = useId();
  const recent = useRecentNodes();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<Awaited<ReturnType<typeof api.suggestLinks>>>([]);
  const [selected, setSelected] = useState(0);
  const items = paletteItems(query, recent, matches);

  useEffect(() => {
    const generation = ++lookupGeneration.current;
    const trimmed = query.trim();
    setMatches([]);
    setSelected(0);
    if (!trimmed) {
      return;
    }
    const controller = new AbortController();
    api
      .suggestLinks(trimmed, 8, controller.signal)
      .then((nodes) => {
        if (!controller.signal.aborted && lookupGeneration.current === generation) setMatches(nodes);
      })
      .catch(() => {
        if (!controller.signal.aborted && lookupGeneration.current === generation) setMatches([]);
      });
    return () => controller.abort();
  }, [query]);

  useEffect(() => setSelected((current) => clampPaletteIndex(current, items.length)), [items.length]);

  useEffect(() => {
    optionRefs.current[selected]?.scrollIntoView({ block: "nearest" });
  }, [selected, items]);

  const execute = (item: PaletteItem | undefined) => {
    if (!item) return;
    if (item.kind === "node" || item.kind === "recent") {
      navigate(`/node/${encodeURIComponent(item.nodeId ?? "")}`);
      onClose();
      return;
    }
    if (item.kind === "new") {
      navigate("/editor");
      onClose();
      return;
    }
    if (item.kind === "cycle") {
      void api
        .runCycle({ dry_run: true })
        .then(() => {
          toast.show("success", "Cycle rehearsal recorded", "No graph changes were made.");
          navigate("/journal");
        })
        .catch((error: unknown) => toast.showError(error));
      onClose();
      return;
    }
    navigate(`/${item.id.slice("view:".length)}`);
    onClose();
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((current) => nextPaletteIndex(current, event.key as "ArrowDown" | "ArrowUp", items.length));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      execute(items[selected]);
    }
  };

  const onQueryChange = (nextQuery: string) => {
    // Clear synchronously with the text change: an old node must not remain
    // visible or executable during the render before the lookup effect starts.
    lookupGeneration.current += 1;
    setMatches([]);
    setSelected(0);
    setQuery(nextQuery);
  };

  return (
    <Modal title="Command palette" onClose={onClose} initialFocus={inputRef}>
      <div className="nd-palette">
        <label className="nd-sr-only" htmlFor="command-palette-input">
          Find a command or node
        </label>
        <input
          ref={inputRef}
          id="command-palette-input"
          name="command"
          type="search"
          className="nd-input nd-palette__input"
          value={query}
          placeholder="Find a command or node…"
           role="combobox"
           aria-autocomplete="list"
           aria-controls={listId}
           aria-expanded={true}
           aria-activedescendant={items[selected] ? `palette-option-${items[selected].id}` : undefined}
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <ul id={listId} className="nd-palette__list" role="listbox" aria-label="Commands and nodes">
          {items.map((item, index) => (
            <li
              id={`palette-option-${item.id}`}
              key={item.id}
              className={index === selected ? "nd-palette__option nd-palette__option--selected" : "nd-palette__option"}
              role="option"
              aria-selected={index === selected}
              ref={(element) => {
                optionRefs.current[index] = element;
              }}
              onMouseMove={() => setSelected(index)}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => execute(item)}
            >
              <span>{item.label}</span>
              <small>{item.detail}</small>
            </li>
          ))}
        </ul>
        {items.length === 0 ? <p className="nd-meta" role="status">No commands or nodes match that search.</p> : null}
      </div>
    </Modal>
  );
}
