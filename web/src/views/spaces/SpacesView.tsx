/**
 * Route `/spaces` — what territory exists (design decision D2).
 *
 * A space is a node of builtin type `space` living in meta, so its whole
 * lifecycle is an ordinary node's: create, a title update, a state transition.
 * That is exactly why this screen exists rather than the alternatives. Folding
 * it into `/admin` would frame a space as pure governance, and `/admin` is
 * already the grants screen; editing spaces as ordinary meta nodes in the
 * editor would be elegant and would leave the human nowhere to see how much
 * lives in a space or who else can reach it.
 *
 * So the two derived facts are the point of the screen, not decoration: the
 * **live node count** (`active` + `proposed` — a space holding only an agent's
 * proposals is not empty) and the **agents holding grants**, with their level.
 * A screen listing only names would answer "what did I call things?" instead of
 * "what territory exists?", which is the question the human currently cannot
 * ask at all.
 *
 * Everything here is a thin call into `/api/spaces`, which is a thin delegate
 * over `service.create_space` / `rename_space` / `archive_space` / `list_spaces`
 * — this view adds no authority the CLI does not have.
 *
 * Archived spaces are absent, and stay absent deliberately. They keep their
 * names (a space title is reserved for good), so a create here can be refused
 * for a name no row on this screen carries — but a row for one would offer
 * nothing this view can do: `POST /api/spaces/{id}/rename` resolves live spaces
 * only (`service._resolve_space` matches `active`), so the one action that
 * would free the name is not on this surface at all, and a listing whose only
 * button fails is the opaque failure again, one layer down. The server's
 * refusal carries the whole answer instead: it says the holder is archived and
 * names it.
 */

import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState, Spinner, useSpaces, useToast } from "../../components";
import { api } from "../../api/client";
import { clearWriteTarget, describeFailure, useWriteTarget } from "../../lib";
import { describeSpaceFailure, renameConsequence, spaceRows, validateSpaceName } from "./spaces";
import type { SpaceRow } from "./spaces";
import { SpaceCard } from "./SpaceCard";
import "./spaces.css";

/** The spaces route. Default-exported because the route is lazily loaded. */
export default function SpacesView() {
  const toast = useToast();
  const { spaces, failed, error: loadError, reload } = useSpaces();
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [writeTarget] = useWriteTarget();
  /**
   * Where the keyboard goes after a card disappears under it.
   *
   * A confirmed archive unmounts the button that opened the dialog, and `Modal`
   * deliberately declines to restore focus to a detached element — focusing one
   * drops the user on `<body>`. The heading is the top of the list they just
   * changed, which is the only place on this screen that is still there.
   */
  const heading = useRef<HTMLHeadingElement>(null);

  // The list *is* this screen, so a failed read is the screen's failure rather
  // than a degraded control the way it is in a filter row.
  const failure = failed ? describeFailure(loadError, "the space list") : null;
  const rows = spaces === null ? null : spaceRows(spaces, writeTarget);
  // Blank is the resting state of an empty field, not a mistake to report; the
  // create button is disabled for it either way.
  const nameError = newName.trim() === "" ? null : validateSpaceName(newName, spaces ?? []);

  /** Report a failure in words that never claim the space is missing. */
  const report = (error: unknown, spaceRef: string) => {
    const described = describeSpaceFailure(error, spaceRef);
    toast.show("error", described.title, described.body);
  };

  const create = () => {
    const name = newName.trim();
    if (name === "" || nameError !== null) return;
    setCreating(true);
    void api.createSpace(name).then(
      async () => {
        setCreating(false);
        setNewName("");
        await reload();
        toast.show(
          "success",
          "Space created",
          `${name} can now be named by --space, by a grant, and by the space filter. No agent ` +
            `reaches it until you grant one in Admin.`,
        );
      },
      (error: unknown) => {
        setCreating(false);
        report(error, name);
      },
    );
  };

  const rename = async (row: SpaceRow, name: string) => {
    await api.renameSpace(row.id, name);
    await reload();
    toast.show("success", "Space renamed", renameConsequence(row, name));
  };

  const archive = async (row: SpaceRow) => {
    await api.archiveSpace(row.id);
    // The write target is stored verbatim and survives the space it names, on
    // purpose — silently rewriting it would file work somewhere the human never
    // chose. Resetting it *here* is the exception the store documents: the
    // human just retired that space themselves, and is told it happened.
    const wasTarget = row.writeTarget;
    if (wasTarget) clearWriteTarget();
    await reload();
    heading.current?.focus();
    // The count was in the confirm the human just read; repeating it here only
    // invites a grammar branch for the empty case. What the toast has to carry
    // is the fact people expect to be false.
    toast.show(
      "success",
      "Space archived",
      `${row.label} is out of the vocabulary. Nothing in it was deleted — every node keeps its ` +
        "space_id and stays exactly as readable as it was." +
        (wasTarget ? " Your write target is back to main." : ""),
    );
  };

  return (
    <div className="nd-view nd-sp">
      <header className="nd-view__header">
        <div>
          <h1 ref={heading} tabIndex={-1}>
            Spaces
          </h1>
          <p className="nd-meta nd-sp__subtitle">
            Every active space, how much lives in it, and which agents hold a grant on it. A space
            is where a node goes, never who wrote it — grant an agent one in{" "}
            <Link to="/admin">Admin</Link>. Archiving retires a space from the vocabulary and
            deletes nothing that is in it; the name it was retired under stays reserved.
          </p>
        </div>
      </header>

      {failure ? (
        <EmptyState title={failure.title} body={failure.body} />
      ) : rows === null ? (
        <div className="nd-empty">
          <Spinner large label="Loading spaces" />
        </div>
      ) : (
        <>
          <section className="nd-sp-create">
            <div className="nd-row">
              <input
                name="new-space-name"
                className="nd-input"
                type="text"
                placeholder="New space name"
                aria-label="New space name"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") create();
                }}
              />
              <button
                type="button"
                className="nd-button nd-button--primary"
                onClick={create}
                disabled={creating || newName.trim() === "" || nameError !== null}
              >
                {creating ? "Creating…" : "Create space"}
              </button>
            </div>
            {nameError ? <p className="nd-sp-create__error">{nameError}</p> : null}
            {rows.length > 0 && rows.every((row) => row.structural) ? (
              <p className="nd-meta nd-sp-create__hint">
                Only the two structural spaces exist. Create one to give a project, a source, or an
                agent territory of its own.
              </p>
            ) : null}
          </section>

          {rows.map((row) => (
            <SpaceCard
              key={row.id}
              row={row}
              spaces={spaces ?? []}
              onRename={rename}
              onArchive={archive}
            />
          ))}
        </>
      )}
    </div>
  );
}

export { SpacesView };
